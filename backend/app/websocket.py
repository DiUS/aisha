import json
import logging
import os
import traceback
import time
from datetime import datetime
from decimal import Decimal as decimal

import boto3
from app.agents.agent import AgentExecutor, create_react_agent, format_log_to_str
from app.agents.handlers.apigw_websocket import ApigwWebsocketCallbackHandler
from app.agents.handlers.token_count import get_token_count_callback
from app.agents.handlers.used_chunk import get_used_chunk_callback
from app.agents.langchain import BedrockLLM
from app.agents.tools.knowledge import AnswerWithKnowledgeTool
from app.agents.utils import get_tool_by_name
from app.auth import verify_token
from app.bedrock import compose_args_for_converse_api, call_converse_api, ConverseApiRequest, ConverseApiResponse, get_model_id
from app.repositories.conversation import RecordNotFoundError, store_conversation
from app.repositories.models.conversation import ChunkModel, ContentModel, MessageModel
from app.routes.schemas.conversation import ChatInput
from app.stream import ConverseApiStreamHandler, OnStopInput
from app.usecases.bot import modify_bot_last_used_time
from app.usecases.chat import insert_knowledge, prepare_conversation, trace_to_root
from app.utils import get_current_time
from app.vector_search import filter_used_results, get_source_link, search_related_docs
from boto3.dynamodb.conditions import Attr, Key
from ulid import ULID

WEBSOCKET_SESSION_TABLE_NAME = os.environ["WEBSOCKET_SESSION_TABLE_NAME"]

dynamodb_client = boto3.resource("dynamodb")
table = dynamodb_client.Table(WEBSOCKET_SESSION_TABLE_NAME)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def invoke_bedrock_with_retries(args: ConverseApiRequest, try_count: int = 1) -> ConverseApiResponse:
    """Invoke Bedrock with retries."""
    max_retries: int = 3
    try:
        response = call_converse_api(args)
    except Exception as e:
        logger.error(f"Failed to invoke bedrock: {e}")
        if try_count > max_retries:
            raise e
        if "throttling" in str(e):
            time.sleep(try_count * 5)
            return invoke_bedrock_with_retries(args, try_count=try_count + 1)
        raise e
    return response


def get_rag_query(conversation, user_msg_id, chat_input, model=None):
    """Get query for RAG model."""
    query = ""

    model = "claude-v3-sonnet" if model is None else model

    messages = trace_to_root(
        node_id=chat_input.message.parent_message_id,
        message_map=conversation.message_map,
    )

    formatted_conversation = ""
    for message in messages:
        if message.role == "user":
            formatted_conversation += f"User: {message.content[-1].body}\n\n"
        if message.role == "assistant":
            formatted_conversation += f"Assistant: {message.content[-1].body}\n\n"
    formatted_conversation += f"User: {chat_input.message.content[-1].body}\n\n"

    # Ask the model what product are we taling about
    template = """
        Based on the following conversation:
        {}

        What is the relevant information to give to the vector search engine?

        Here are a few examples of how you can respond:
        <examples>
            <example>
                <input>
                    User: Id like to buy in iphone.
                    Assistant: Sure, which model are you interested in?
                    User: I am interested in iPhone 13.
                </input>
                <output>
                    "iPhone 13"
                </output>
            </example>
            <example>
                <input>
                    User: I am interested in a tshirt.
                    Assistant: Okay, I'd be happy to help you find a t-shirt! To narrow down the options, could you provide some more details? What style of t-shirt are you looking for - casual, athletic, graphic print? Do you have a preferred fit like slim, relaxed, or loose? And what size would you need? Any particular colors or designs you're interested in? The more specifics you can give me, the better I can suggest some relevant options from the available products
                    User: casual, black, vneck, slim, L.
                </input>
                <output>
                    "Black casual vneck large tshirt"
                </output>
            </example>
            <example>
                <input>
                    User: I need a new job.
                    Assistant: To better assist you in finding relevant engineering job opportunities, could you please provide some more details? What specific type of engineering role are you interested in (software, mechanical, civil, etc.)? Do you have a preferred location or are you open to remote opportunities? Any particular industry or company you’d like to target? The more specifics you can provide, the better I can narrow down the options from the opportunities I have available.
                    User: software.
                </input>
                <output>
                    "Software engineering job"
                </output>
            </example>
            <example>
                <input>
                    User: I need a software engineering job.
                    Assistant: Here are some relevant software engineering job opportunities in Bengaluru that I can suggest based on the provided contexts:
                        Staff Software Engineer
                        Software Engineer
                        Senior Software Engineer in Test
                    User: Give me details about the third option.
                </input>
                <output>
                    "Senior Software Engineer in Test"
                </output>
            </example>
        </examples>

        If there are multiple subjects, provide them all. If there is no specific subject, give as much details and characteristics about what the user is looking for.
        Format your answer as a single line of text.
        """.format(formatted_conversation)

    # Invoke Bedrock
    args = compose_args_for_converse_api(
        messages=[
            MessageModel(
                role="user",
                content=[ContentModel(content_type="text", body=template, media_type=None)],
                model=model,
                children=[],
                parent=None,
                feedback=None,
                used_chunks=None,
                create_time=get_current_time(),
            ),
        ],
        model=model,
        instruction="""
            You are an helpful assistant that whose job is to understand what the user is enquirying about.
        """,
        stream=False,
    )
    try:
        # Invoke bedrock api
        response = invoke_bedrock_with_retries(args)
        # Use the product name returned by the LLM
        logger.info(f"Bedrock response: {response}")
        query = response['output']['message']['content'][0]['text']
        return query
    except Exception as e:
        logger.error(f"Failed to invoke bedrock: {e}")
        # Use the last user message as the query
        return (
            conversation
            .message_map[user_msg_id]
            .content[-1]
            .body
        )


def process_chat_input(
    user_id: str, chat_input: ChatInput, gatewayapi, connection_id: str
) -> dict:
    """Process chat input and send the message to the client."""
    logger.info(f"Received chat input: {chat_input}")

    try:
        user_msg_id, conversation, bot = prepare_conversation(user_id, chat_input)
    except RecordNotFoundError:
        if chat_input.bot_id:
            gatewayapi.post_to_connection(
                ConnectionId=connection_id,
                Data=json.dumps(
                    dict(
                        status="ERROR",
                        reason="bot_not_found",
                    )
                ).encode("utf-8"),
            )
            return {"statusCode": 404, "body": f"bot {chat_input.bot_id} not found."}
        else:
            return {"statusCode": 400, "body": "Invalid request."}

    logger.info(f"Found bot: {bot}")
    if bot and bot.is_agent_enabled():
        logger.info("Bot has agent tools. Using agent for response.")
        llm = BedrockLLM.from_model(model=chat_input.message.model)

        tools = [get_tool_by_name(t.name) for t in bot.agent.tools]

        if bot and bot.has_knowledge():
            logger.info("Bot has knowledge. Adding answer with knowledge tool.")
            answer_with_knowledge_tool = AnswerWithKnowledgeTool.from_bot(
                bot=bot,
                llm=llm,
            )
            tools.append(answer_with_knowledge_tool)

        logger.info(f"Tools: {tools}")
        agent = create_react_agent(
            model=chat_input.message.model,
            tools=tools,
            generation_config=bot.generation_params,
        )
        executor = AgentExecutor(
            name="Agent Executor",
            agent=agent,
            tools=tools,
            return_intermediate_steps=True,
            callbacks=[],
            verbose=False,
            max_iterations=15,
            max_execution_time=None,
            early_stopping_method="force",
            handle_parsing_errors=True,
        )

        price = 0.0
        used_chunks = None
        thinking_log = None
        with get_token_count_callback() as token_cb, get_used_chunk_callback() as chunk_cb:
            response = executor.invoke(
                {
                    "input": chat_input.message.content[0].body,
                },
                config={
                    "callbacks": [
                        ApigwWebsocketCallbackHandler(gatewayapi, connection_id),
                        token_cb,
                        chunk_cb,
                    ],
                },
            )
            price = token_cb.total_cost
            if bot.display_retrieved_chunks and chunk_cb.used_chunks:
                used_chunks = chunk_cb.used_chunks
            thinking_log = format_log_to_str(response.get("intermediate_steps", []))
            logger.info(f"Thinking log: {thinking_log}")

        # Append entire completion as the last message
        assistant_msg_id = str(ULID())
        message = MessageModel(
            role="assistant",
            content=[
                ContentModel(
                    content_type="text",
                    body=response["output"],
                    media_type=None,
                    file_name=None,
                )
            ],
            model=chat_input.message.model,
            children=[],
            parent=user_msg_id,
            create_time=get_current_time(),
            feedback=None,
            used_chunks=used_chunks,
            thinking_log=thinking_log,
        )
        conversation.message_map[assistant_msg_id] = message
        # Append children to parent
        conversation.message_map[user_msg_id].children.append(assistant_msg_id)
        conversation.last_message_id = assistant_msg_id

        conversation.total_price += price

        # Store conversation before finish streaming so that front-end can avoid 404 issue
        store_conversation(user_id, conversation)

        # Send signal so that frontend can close the connection
        last_data_to_send = json.dumps(
            dict(status="STREAMING_END", completion="", stop_reason="agent_finish")
        ).encode("utf-8")
        gatewayapi.post_to_connection(
            ConnectionId=connection_id, Data=last_data_to_send
        )

        return {"statusCode": 200, "body": "Message sent."}

    message_map = conversation.message_map
    search_results = []
    if bot and bot.has_knowledge():
        gatewayapi.post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps(
                dict(
                    status="FETCHING_KNOWLEDGE",
                )
            ).encode("utf-8"),
        )

        # Fetch most related documents from vector store
        # NOTE: Currently embedding not support multi-modal. For now, use the last text content.
        # query: str = conversation.message_map[user_msg_id].content[-1].body  # type: ignore[assignment]
        query = get_rag_query(
            conversation,
            user_msg_id,
            chat_input,
            chat_input.message.model,
        )
        logger.info(f"Query for RAG model: {query}")
        search_results = search_related_docs(bot=bot, query=query)
        logger.info(f"Search results from vector store: {search_results}")

        # Insert contexts to instruction
        conversation_with_context = insert_knowledge(
            conversation, search_results, display_citation=bot.display_retrieved_chunks
        )
        message_map = conversation_with_context.message_map

    messages = trace_to_root(
        node_id=chat_input.message.parent_message_id,
        message_map=message_map,
    )

    if not chat_input.continue_generate:
        messages.append(chat_input.message)  # type: ignore

    args = compose_args_for_converse_api(
        messages,
        chat_input.message.model,
        instruction=(
            message_map["instruction"].content[0].body  # type: ignore[union-attr]
            if "instruction" in message_map
            else None
        ),
        stream=True,
        generation_params=(bot.generation_params if bot else None),
        guardrail_config=(bot.guardrail_config if bot else None),
    )

    def on_stream(token: str, **kwargs) -> None:
        # Send completion
        data_to_send = json.dumps(dict(status="STREAMING", completion=token)).encode(
            "utf-8"
        )
        gatewayapi.post_to_connection(ConnectionId=connection_id, Data=data_to_send)

    def on_stop(arg: OnStopInput, **kwargs) -> None:
        if chat_input.continue_generate:
            # For continue generate
            conversation.message_map[conversation.last_message_id].content[
                0
            ].body += arg.full_token  # type: ignore[operator]
        else:
            used_chunks = None
            if bot and bot.display_retrieved_chunks:
                if len(search_results) > 0:
                    used_chunks = []
                    for r in filter_used_results(arg.full_token, search_results):
                        content_type, source_link = get_source_link(r.source)
                        used_chunks.append(
                            ChunkModel(
                                content=r.content,
                                content_type=content_type,
                                source=source_link,
                                rank=r.rank,
                            )
                        )

            # Append entire completion as the last message
            assistant_msg_id = str(ULID())
            message = MessageModel(
                role="assistant",
                content=[
                    ContentModel(
                        content_type="text",
                        body=arg.full_token,
                        media_type=None,
                        file_name=None,
                    )
                ],
                model=chat_input.message.model,
                children=[],
                parent=user_msg_id,
                create_time=get_current_time(),
                feedback=None,
                used_chunks=used_chunks,
                thinking_log=None,
            )
            conversation.message_map[assistant_msg_id] = message
            # Append children to parent
            conversation.message_map[user_msg_id].children.append(assistant_msg_id)
            conversation.last_message_id = assistant_msg_id

        conversation.total_price += arg.price

        # If continued, save the state
        conversation.should_continue = arg.stop_reason == "max_tokens"

        # Guardrail intervened
        if arg.stop_reason == "guardrail_intervened":
            logger.error(f"Guardrail intervened. {arg.trace}")

        # Store conversation before finish streaming so that front-end can avoid 404 issue
        store_conversation(user_id, conversation)
        last_data_to_send = json.dumps(
            dict(status="STREAMING_END", completion="", stop_reason=arg.stop_reason)
        ).encode("utf-8")
        gatewayapi.post_to_connection(
            ConnectionId=connection_id, Data=last_data_to_send
        )

    stream_handler = ConverseApiStreamHandler(
        model=chat_input.message.model,
        on_stream=on_stream,
        on_stop=on_stop,
    )
    try:
        logger.info(f"Running stream handler with args: {args}")
        for _ in stream_handler.run(args):
            # `StreamHandler.run` returns a generator, so need to iterate
            ...
    except Exception as e:
        logger.error(f"Failed to run stream handler: {e}")
        return {
            "statusCode": 500,
            "body": "Failed to run stream handler.",
        }

    # Update bot last used time
    if chat_input.bot_id:
        logger.info("Bot id is provided. Updating bot last used time.")
        modify_bot_last_used_time(user_id, chat_input.bot_id)

    return {"statusCode": 200, "body": "Message sent."}


def handler(event, context):
    logger.info(f"Received event: {event}")
    route_key = event["requestContext"]["routeKey"]

    if route_key == "$connect":
        return {"statusCode": 200, "body": "Connected."}
    elif route_key == "$disconnect":
        return {"statusCode": 200, "body": "Disconnected."}

    connection_id = event["requestContext"]["connectionId"]
    domain_name = event["requestContext"]["domainName"]
    stage = event["requestContext"]["stage"]
    endpoint_url = f"https://{domain_name}/{stage}"
    gatewayapi = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint_url)

    now = datetime.now()
    expire = int(now.timestamp()) + 60 * 2  # 2 minute from now
    body = json.loads(event["body"])
    step = body.get("step")

    try:
        # API Gateway (websocket) has hard limit of 32KB per message, so if the message is larger than that,
        # need to concatenate chunks and send as a single full message.
        # To do that, we store the chunks in DynamoDB and when the message is complete, send it to SNS.
        # The life cycle of the message is as follows:
        # 1. Client sends `START` message to the WebSocket API.
        # 2. This handler receives the `Session started` message.
        # 3. Client sends message parts to the WebSocket API.
        # 4. This handler receives the message parts and appends them to the item in DynamoDB with index.
        # 5. Client sends `END` message to the WebSocket API.
        # 6. This handler receives the `END` message, concatenates the parts and sends the message to Bedrock.
        if step == "START":
            token = body["token"]
            try:
                # Verify JWT token
                decoded = verify_token(token)
            except Exception as e:
                logger.error(f"Invalid token: {e}")
                return {"statusCode": 403, "body": "Invalid token."}
            user_id = decoded["sub"]

            # Store user id
            response = table.put_item(
                Item={
                    "ConnectionId": connection_id,
                    # Store as zero
                    "MessagePartId": decimal(0),
                    "UserId": user_id,
                    "expire": expire,
                }
            )
            return {"statusCode": 200, "body": "Session started."}
        elif step == "END":
            # Retrieve user id
            response = table.query(
                KeyConditionExpression=Key("ConnectionId").eq(connection_id),
                FilterExpression=Attr("UserId").exists(),
            )
            user_id = response["Items"][0]["UserId"]

            # Concatenate the message parts
            message_parts = []
            last_evaluated_key = None

            while True:
                if last_evaluated_key:
                    response = table.query(
                        KeyConditionExpression=Key("ConnectionId").eq(connection_id)
                        # Zero is reserved for user id, so start from 1
                        & Key("MessagePartId").gte(1),
                        ExclusiveStartKey=last_evaluated_key,
                    )
                else:
                    response = table.query(
                        KeyConditionExpression=Key("ConnectionId").eq(connection_id)
                        & Key("MessagePartId").gte(1),
                    )

                message_parts.extend(response["Items"])

                if "LastEvaluatedKey" in response:
                    last_evaluated_key = response["LastEvaluatedKey"]
                else:
                    break

            logger.info(f"Number of message chunks: {len(message_parts)}")
            message_parts.sort(key=lambda x: x["MessagePartId"])
            full_message = "".join(item["MessagePart"] for item in message_parts)

            # Process the concatenated full message
            chat_input = ChatInput(**json.loads(full_message))
            return process_chat_input(
                user_id=user_id,
                chat_input=chat_input,
                gatewayapi=gatewayapi,
                connection_id=connection_id,
            )
        else:
            # Store the message part of full message
            # Zero is reserved for user id, so start from 1
            part_index = body["index"] + 1
            message_part = body["part"]

            # Store the message part with its index
            table.put_item(
                Item={
                    "ConnectionId": connection_id,
                    "MessagePartId": decimal(part_index),
                    "MessagePart": message_part,
                    "expire": expire,
                }
            )
            return {"statusCode": 200, "body": "Message part received."}

    except Exception as e:
        logger.error(f"Operation failed: {e}")
        logger.error("".join(traceback.format_tb(e.__traceback__)))
        gatewayapi.post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps({"status": "ERROR", "reason": str(e)}).encode("utf-8"),
        )
        return {"statusCode": 500, "body": str(e)}
