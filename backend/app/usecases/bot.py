import logging
import os

from app.agents.utils import get_available_tools, get_tool_by_name
from app.config import DEFAULT_EMBEDDING_CONFIG
from app.config import DEFAULT_GENERATION_CONFIG as DEFAULT_CLAUDE_GENERATION_CONFIG
from app.config import DEFAULT_MISTRAL_GENERATION_CONFIG, DEFAULT_SEARCH_CONFIG
from app.repositories.common import (
    RecordNotFoundError,
    _get_table_client,
    decompose_bot_alias_id,
    decompose_bot_id,
)
from app.repositories.custom_bot import (
    delete_alias_by_id,
    delete_bot_by_id,
    find_alias_by_id,
    find_private_bot_by_id,
    find_public_bot_by_id,
    store_alias,
    store_bot,
    update_alias_last_used_time,
    update_alias_pin_status,
    update_bot,
    update_bot_last_used_time,
    update_bot_pin_status,
)
from app.repositories.models.custom_bot import (
    AgentModel,
    AgentToolModel,
    BotAliasModel,
    BotMeta,
    BotModel,
    ConversationQuickStarterModel,
    EmbeddingParamsModel,
    GenerationParamsModel,
    KnowledgeModel,
    SearchParamsModel,
    GuardrailConfig
)
from app.repositories.models.custom_bot_kb import BedrockKnowledgeBaseModel
from app.routes.schemas.bot import (
    Agent,
    AgentTool,
    BotInput,
    BotModifyInput,
    BotModifyOutput,
    BotOutput,
    BotSummaryOutput,
    GuardrailListOutput,
    GuardrailDataOutput,
    ConversationQuickStarter,
    EmbeddingParams,
    GenerationParams,
    Knowledge,
    SearchParams,
    type_sync_status,
)
from app.routes.schemas.bot_kb import BedrockKnowledgeBaseOutput
from app.utils import (
    compose_upload_document_s3_path,
    compose_upload_temp_s3_path,
    compose_upload_temp_s3_prefix,
    delete_file_from_s3,
    delete_files_with_prefix_from_s3,
    generate_presigned_url,
    get_current_time,
    move_file_in_s3,
    list_guardrails
)
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

DOCUMENT_BUCKET = os.environ.get("DOCUMENT_BUCKET", "bedrock-documents")
ENABLE_MISTRAL = os.environ.get("ENABLE_MISTRAL", "") == "true"

DEFAULT_GENERATION_CONFIG = (
    DEFAULT_MISTRAL_GENERATION_CONFIG
    if ENABLE_MISTRAL
    else DEFAULT_CLAUDE_GENERATION_CONFIG
)


def _update_s3_documents_by_diff(
    user_id: str,
    bot_id: str,
    added_filenames: list[str],
    deleted_filenames: list[str],
):
    for filename in added_filenames:
        tmp_path = compose_upload_temp_s3_path(user_id, bot_id, filename)
        document_path = compose_upload_document_s3_path(user_id, bot_id, filename)
        move_file_in_s3(DOCUMENT_BUCKET, tmp_path, document_path)

    for filename in deleted_filenames:
        document_path = compose_upload_document_s3_path(user_id, bot_id, filename)
        delete_file_from_s3(DOCUMENT_BUCKET, document_path)


def create_new_bot(user_id: str, bot_input: BotInput) -> BotOutput:
    """Create a new bot.
    Bot is created as private and not pinned.
    """
    current_time = get_current_time()
    has_knowledge = bot_input.knowledge and (
        len(bot_input.knowledge.source_urls) > 0
        or len(bot_input.knowledge.sitemap_urls) > 0
        or len(bot_input.knowledge.filenames) > 0
        or len(bot_input.knowledge.s3_urls) > 0
    )
    sync_status: type_sync_status = "QUEUED" if has_knowledge else "SUCCEEDED"

    source_urls = []
    sitemap_urls = []
    filenames = []
    s3_urls = []
    if bot_input.knowledge:
        source_urls = bot_input.knowledge.source_urls
        sitemap_urls = bot_input.knowledge.sitemap_urls
        s3_urls = bot_input.knowledge.s3_urls

        # Commit changes to S3
        _update_s3_documents_by_diff(
            user_id, bot_input.id, bot_input.knowledge.filenames, []
        )
        # Delete files from upload temp directory
        delete_files_with_prefix_from_s3(
            DOCUMENT_BUCKET, compose_upload_temp_s3_prefix(user_id, bot_input.id)
        )
        filenames = bot_input.knowledge.filenames

    chunk_size = (
        bot_input.embedding_params.chunk_size
        if bot_input.embedding_params
        else DEFAULT_EMBEDDING_CONFIG["chunk_size"]
    )

    chunk_overlap = (
        bot_input.embedding_params.chunk_overlap
        if bot_input.embedding_params
        else DEFAULT_EMBEDDING_CONFIG["chunk_overlap"]
    )

    enable_partition_pdf = (
        bot_input.embedding_params.enable_partition_pdf
        if bot_input.embedding_params
        else DEFAULT_EMBEDDING_CONFIG["enable_partition_pdf"]
    )

    generation_params = (
        bot_input.generation_params.model_dump()
        if bot_input.generation_params
        else DEFAULT_GENERATION_CONFIG
    )

    search_params = (
        bot_input.search_params.model_dump()
        if bot_input.search_params
        else DEFAULT_SEARCH_CONFIG
    )

    agent = (
        AgentModel(
            tools=[
                AgentToolModel(name=t.name, description=t.description)
                for t in [
                    get_tool_by_name(tool_name) for tool_name in bot_input.agent.tools
                ]
            ]
        )
        if bot_input.agent
        else AgentModel(tools=[])
    )

    store_bot(
        user_id,
        BotModel(
            id=bot_input.id,
            title=bot_input.title,
            description=bot_input.description if bot_input.description else "",
            instruction=bot_input.instruction,
            create_time=current_time,
            last_used_time=current_time,
            public_bot_id=None,
            is_pinned=False,
            owner_user_id=user_id,  # Owner is the creator
            embedding_params=EmbeddingParamsModel(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                enable_partition_pdf=enable_partition_pdf,
            ),
            generation_params=GenerationParamsModel(**generation_params),  # type: ignore
            search_params=SearchParamsModel(**search_params),
            agent=agent,
            knowledge=KnowledgeModel(
                source_urls=source_urls,
                sitemap_urls=sitemap_urls,
                filenames=filenames,
                s3_urls=s3_urls,
            ),
            sync_status=sync_status,
            sync_status_reason="",
            sync_last_exec_id="",
            published_api_stack_name=None,
            published_api_datetime=None,
            published_api_codebuild_id=None,
            display_retrieved_chunks=bot_input.display_retrieved_chunks,
            conversation_quick_starters=(
                []
                if bot_input.conversation_quick_starters is None
                else [
                    ConversationQuickStarterModel(
                        title=starter.title,
                        example=starter.example,
                    )
                    for starter in bot_input.conversation_quick_starters
                ]
            ),
            bedrock_knowledge_base=(
                BedrockKnowledgeBaseModel(
                    **(bot_input.bedrock_knowledge_base.model_dump())
                )
                if bot_input.bedrock_knowledge_base
                else None
            ),
            guardrail_config=GuardrailConfig(
                **(bot_input.guardrail_config.model_dump())
                if bot_input.guardrail_config
                else None
            )
        ),
    )
    return BotOutput(
        id=bot_input.id,
        title=bot_input.title,
        instruction=bot_input.instruction,
        description=bot_input.description if bot_input.description else "",
        create_time=current_time,
        last_used_time=current_time,
        is_public=False,
        is_pinned=False,
        owned=True,
        embedding_params=EmbeddingParams(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            enable_partition_pdf=enable_partition_pdf,
        ),
        generation_params=GenerationParams(**generation_params),
        search_params=SearchParams(**search_params),
        agent=Agent(
            tools=[
                AgentTool(name=tool.name, description=tool.description)
                for tool in agent.tools
            ]
        ),
        knowledge=Knowledge(
            source_urls=source_urls,
            sitemap_urls=sitemap_urls,
            filenames=filenames,
            s3_urls=s3_urls,
        ),
        sync_status=sync_status,
        sync_status_reason="",
        sync_last_exec_id="",
        display_retrieved_chunks=bot_input.display_retrieved_chunks,
        conversation_quick_starters=(
            []
            if bot_input.conversation_quick_starters is None
            else [
                ConversationQuickStarter(
                    title=starter.title,
                    example=starter.example,
                )
                for starter in bot_input.conversation_quick_starters
            ]
        ),
        bedrock_knowledge_base=(
            BedrockKnowledgeBaseOutput(
                **(bot_input.bedrock_knowledge_base.model_dump())
            )
            if bot_input.bedrock_knowledge_base
            else None
        ),
        guardrail_config=bot_input.guardrail_config,
    )


def modify_owned_bot(
    user_id: str, bot_id: str, modify_input: BotModifyInput
) -> BotModifyOutput:
    """Modify owned bot."""
    source_urls = []
    sitemap_urls = []
    filenames = []
    s3_urls = []
    sync_status: type_sync_status = "QUEUED"

    if modify_input.knowledge:
        source_urls = modify_input.knowledge.source_urls
        sitemap_urls = modify_input.knowledge.sitemap_urls
        s3_urls = modify_input.knowledge.s3_urls

        # Commit changes to S3
        _update_s3_documents_by_diff(
            user_id,
            bot_id,
            modify_input.knowledge.added_filenames,
            modify_input.knowledge.deleted_filenames,
        )
        # Delete files from upload temp directory
        delete_files_with_prefix_from_s3(
            DOCUMENT_BUCKET, compose_upload_temp_s3_prefix(user_id, bot_id)
        )

        filenames = (
            modify_input.knowledge.added_filenames
            + modify_input.knowledge.unchanged_filenames
        )

    chunk_size = (
        modify_input.embedding_params.chunk_size
        if modify_input.embedding_params
        else DEFAULT_EMBEDDING_CONFIG["chunk_size"]
    )

    chunk_overlap = (
        modify_input.embedding_params.chunk_overlap
        if modify_input.embedding_params
        else DEFAULT_EMBEDDING_CONFIG["chunk_overlap"]
    )

    enable_partition_pdf = (
        modify_input.embedding_params.enable_partition_pdf
        if modify_input.embedding_params
        else DEFAULT_EMBEDDING_CONFIG["enable_partition_pdf"]
    )

    generation_params = (
        modify_input.generation_params.model_dump()
        if modify_input.generation_params
        else DEFAULT_GENERATION_CONFIG
    )

    search_params = (
        modify_input.search_params.model_dump()
        if modify_input.search_params
        else DEFAULT_SEARCH_CONFIG
    )

    agent = (
        AgentModel(
            tools=[
                AgentToolModel(name=t.name, description=t.description)
                for t in [
                    get_tool_by_name(tool_name)
                    for tool_name in modify_input.agent.tools
                ]
            ]
        )
        if modify_input.agent
        else AgentModel(tools=[])
    )

    # if knowledge and embedding_params are not updated, skip embeding process.
    # 'sync_status = "QUEUED"' will execute embeding process and update dynamodb record.
    # 'sync_status= "SUCCEEDED"' will update only dynamodb record.
    bot = find_private_bot_by_id(user_id, bot_id)
    sync_status = "QUEUED" if modify_input.is_embedding_required(bot) else "SUCCEEDED"

    update_bot(
        user_id,
        bot_id,
        title=modify_input.title,
        instruction=modify_input.instruction,
        description=modify_input.description if modify_input.description else "",
        embedding_params=EmbeddingParamsModel(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            enable_partition_pdf=enable_partition_pdf,
        ),
        generation_params=GenerationParamsModel(**generation_params),
        search_params=SearchParamsModel(**search_params),
        agent=agent,
        knowledge=KnowledgeModel(
            source_urls=source_urls,
            sitemap_urls=sitemap_urls,
            filenames=filenames,
            s3_urls=s3_urls,
        ),
        sync_status=sync_status,
        sync_status_reason="",
        display_retrieved_chunks=modify_input.display_retrieved_chunks,
        conversation_quick_starters=(
            []
            if modify_input.conversation_quick_starters is None
            else [
                ConversationQuickStarterModel(
                    title=starter.title,
                    example=starter.example,
                )
                for starter in modify_input.conversation_quick_starters
            ]
        ),
        bedrock_knowledge_base=(
            BedrockKnowledgeBaseModel(
                **modify_input.bedrock_knowledge_base.model_dump()
            )
            if modify_input.bedrock_knowledge_base
            else None
        ),
        guardrail_config=GuardrailConfig(
            **(modify_input.guardrail_config.model_dump())
            if modify_input.guardrail_config
            else None
        )
    )

    return BotModifyOutput(
        id=bot_id,
        title=modify_input.title,
        instruction=modify_input.instruction,
        description=modify_input.description if modify_input.description else "",
        embedding_params=EmbeddingParams(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            enable_partition_pdf=enable_partition_pdf,
        ),
        generation_params=GenerationParams(**generation_params),
        search_params=SearchParams(**search_params),
        agent=Agent(
            tools=[
                AgentTool(name=tool.name, description=tool.description)
                for tool in agent.tools
            ]
        ),
        knowledge=Knowledge(
            source_urls=source_urls,
            sitemap_urls=sitemap_urls,
            filenames=filenames,
            s3_urls=s3_urls,
        ),
        conversation_quick_starters=(
            []
            if modify_input.conversation_quick_starters is None
            else [
                ConversationQuickStarter(
                    title=starter.title,
                    example=starter.example,
                )
                for starter in modify_input.conversation_quick_starters
            ]
        ),
        bedrock_knowledge_base=(
            BedrockKnowledgeBaseOutput(
                **(modify_input.bedrock_knowledge_base.model_dump())
            )
            if modify_input.bedrock_knowledge_base
            else None
        ),
        guardrail_config=modify_input.guardrail_config,
    )


def fetch_bot(user_id: str, bot_id: str) -> tuple[bool, BotModel]:
    """Fetch bot by id.
    The first element of the returned tuple is whether the bot is owned or not.
    `True` means the bot is owned by the user.
    `False` means the bot is shared by another user.
    """
    try:
        return True, find_private_bot_by_id(user_id, bot_id)
    except RecordNotFoundError:
        pass  #

    try:
        return False, find_public_bot_by_id(bot_id)
    except RecordNotFoundError:
        raise RecordNotFoundError(
            f"Bot with ID {bot_id} not found in both private (for user {user_id}) and public items."
        )


def fetch_all_bots_by_user_id(
    user_id: str, limit: int | None = None, only_pinned: bool = False
) -> list[BotMeta]:
    """Find all private & shared bots of a user.
    The order is descending by `last_used_time`.
    """
    if not only_pinned and not limit:
        raise ValueError("Must specify either `limit` or `only_pinned`")
    if limit and only_pinned:
        raise ValueError("Cannot specify both `limit` and `only_pinned`")
    if limit and (limit < 0 or limit > 100):
        raise ValueError("Limit must be between 0 and 100")

    table = _get_table_client(user_id)
    logger.info(f"Finding pinned bots for user: {user_id}")

    # Fetch all pinned bots
    query_params = {
        "IndexName": "LastBotUsedIndex",
        "KeyConditionExpression": Key("PK").eq(user_id),
        "ScanIndexForward": False,
    }
    if limit:
        query_params["Limit"] = limit
    if only_pinned:
        query_params["FilterExpression"] = Attr("IsPinned").eq(True)

    response = table.query(**query_params)

    bots = []
    for item in response["Items"]:
        if "OriginalBotId" in item:
            # Fetch original bots of alias bots
            is_original_available = True
            try:
                bot = find_public_bot_by_id(item["OriginalBotId"])
                logger.info(f"Found original bot: {bot.id}")
                meta = BotMeta(
                    id=bot.id,
                    title=bot.title,
                    create_time=float(bot.create_time),
                    last_used_time=float(bot.last_used_time),
                    is_pinned=item["IsPinned"],
                    owned=False,
                    available=True,
                    description=bot.description,
                    is_public=True,
                    sync_status=bot.sync_status,
                    has_bedrock_knowledge_base=bot.has_bedrock_knowledge_base(),
                )
            except RecordNotFoundError:
                # Original bot is removed
                is_original_available = False
                logger.info(f"Original bot {item['OriginalBotId']} has been removed")
                meta = BotMeta(
                    id=item["OriginalBotId"],
                    title=item["Title"],
                    create_time=float(item["CreateTime"]),
                    last_used_time=float(item["LastBotUsed"]),
                    is_pinned=item["IsPinned"],
                    owned=False,
                    # NOTE: Original bot is removed
                    available=False,
                    description="This item is no longer available",
                    is_public=False,
                    sync_status="ORIGINAL_NOT_FOUND",
                    has_bedrock_knowledge_base=False,
                )

            if is_original_available and (
                bot.title != item["Title"]
                or bot.description != item["Description"]
                or bot.sync_status != item["SyncStatus"]
                or bot.has_knowledge() != item["HasKnowledge"]
                or bot.conversation_quick_starters
                != [
                    ConversationQuickStarter(**starter)
                    for starter in item.get("ConversationQuickStarters", [])
                ]
            ):
                # Update alias to the latest original bot
                store_alias(
                    user_id,
                    BotAliasModel(
                        id=decompose_bot_alias_id(item["SK"]),
                        # Update title and description
                        title=bot.title,
                        description=bot.description,
                        original_bot_id=item["OriginalBotId"],
                        create_time=float(item["CreateTime"]),
                        last_used_time=float(item["LastBotUsed"]),
                        is_pinned=item["IsPinned"],
                        sync_status=bot.sync_status,
                        has_knowledge=bot.has_knowledge(),
                        has_agent=bot.is_agent_enabled(),
                        conversation_quick_starters=bot.conversation_quick_starters,
                    ),
                )

            bots.append(meta)
        else:
            # Private bots
            bots.append(
                BotMeta(
                    id=decompose_bot_id(item["SK"]),
                    title=item["Title"],
                    create_time=float(item["CreateTime"]),
                    last_used_time=float(item["LastBotUsed"]),
                    is_pinned=item["IsPinned"],
                    owned=True,
                    available=True,
                    description=item["Description"],
                    is_public="PublicBotId" in item,
                    sync_status=item["SyncStatus"],
                    has_bedrock_knowledge_base=(
                        True if item.get("BedrockKnowledgeBase", None) else False
                    ),
                )
            )

    return bots


def fetch_bot_summary(user_id: str, bot_id: str) -> BotSummaryOutput:
    try:
        bot = find_private_bot_by_id(user_id, bot_id)
        return BotSummaryOutput(
            id=bot_id,
            title=bot.title,
            description=bot.description,
            create_time=bot.create_time,
            last_used_time=bot.last_used_time,
            is_pinned=bot.is_pinned,
            is_public=True if bot.public_bot_id else False,
            has_agent=bot.is_agent_enabled(),
            owned=True,
            sync_status=bot.sync_status,
            has_knowledge=bot.has_knowledge(),
            conversation_quick_starters=[
                ConversationQuickStarter(
                    title=starter.title,
                    example=starter.example,
                )
                for starter in bot.conversation_quick_starters
            ],
            owned_and_has_bedrock_knowledge_base=bot.has_bedrock_knowledge_base(),
        )

    except RecordNotFoundError:
        pass

    try:
        alias = find_alias_by_id(user_id, bot_id)
        return BotSummaryOutput(
            id=alias.id,
            title=alias.title,
            description=alias.description,
            create_time=alias.create_time,
            last_used_time=alias.last_used_time,
            is_pinned=alias.is_pinned,
            is_public=True,
            has_agent=alias.has_agent,
            owned=False,
            sync_status=alias.sync_status,
            has_knowledge=alias.has_knowledge,
            conversation_quick_starters=(
                []
                if alias.conversation_quick_starters is None
                else [
                    ConversationQuickStarter(
                        title=starter.title,
                        example=starter.example,
                    )
                    for starter in alias.conversation_quick_starters
                ]
            ),
            owned_and_has_bedrock_knowledge_base=False,
        )
    except RecordNotFoundError:
        pass

    try:
        # NOTE: At the first time using shared bot, alias is not created yet.
        bot = find_public_bot_by_id(bot_id)
        current_time = get_current_time()
        # Store alias when opened shared bot page
        store_alias(
            user_id,
            BotAliasModel(
                id=bot.id,
                title=bot.title,
                description=bot.description,
                original_bot_id=bot_id,
                create_time=current_time,
                last_used_time=current_time,
                is_pinned=False,
                sync_status=bot.sync_status,
                has_knowledge=bot.has_knowledge(),
                has_agent=bot.is_agent_enabled(),
                conversation_quick_starters=[
                    ConversationQuickStarterModel(
                        title=starter.title,
                        example=starter.example,
                    )
                    for starter in bot.conversation_quick_starters
                ],
            ),
        )
        return BotSummaryOutput(
            id=bot_id,
            title=bot.title,
            description=bot.description,
            create_time=bot.create_time,
            last_used_time=bot.last_used_time,
            is_pinned=False,  # NOTE: Shared bot is not pinned by default.
            is_public=True,
            has_agent=bot.is_agent_enabled(),
            owned=False,
            sync_status=bot.sync_status,
            has_knowledge=bot.has_knowledge(),
            conversation_quick_starters=[
                ConversationQuickStarter(
                    title=starter.title,
                    example=starter.example,
                )
                for starter in bot.conversation_quick_starters
            ],
            owned_and_has_bedrock_knowledge_base=False,
        )
    except RecordNotFoundError:
        raise RecordNotFoundError(
            f"Bot with ID {bot_id} not found in both private (for user {user_id}) and alias, shared items."
        )


def modify_pin_status(user_id: str, bot_id: str, pinned: bool):
    """Modify bot pin status."""
    try:
        return update_bot_pin_status(user_id, bot_id, pinned)
    except RecordNotFoundError:
        pass

    try:
        return update_alias_pin_status(user_id, bot_id, pinned)
    except RecordNotFoundError:
        raise RecordNotFoundError(f"Bot {bot_id} is neither owned nor alias.")


def remove_bot_by_id(user_id: str, bot_id: str):
    """Remove bot by id."""
    try:
        return delete_bot_by_id(user_id, bot_id)
    except RecordNotFoundError:
        pass

    try:
        return delete_alias_by_id(user_id, bot_id)
    except RecordNotFoundError:
        raise RecordNotFoundError(f"Bot {bot_id} is neither owned nor alias.")


def modify_bot_last_used_time(user_id: str, bot_id: str):
    """Modify bot last used time."""
    try:
        return update_bot_last_used_time(user_id, bot_id)
    except RecordNotFoundError:
        pass

    try:
        return update_alias_last_used_time(user_id, bot_id)
    except RecordNotFoundError:
        raise RecordNotFoundError(f"Bot {bot_id} is neither owned nor alias.")


def issue_presigned_url(
    user_id: str, bot_id: str, filename: str, content_type: str
) -> str:
    response = generate_presigned_url(
        DOCUMENT_BUCKET,
        compose_upload_temp_s3_path(user_id, bot_id, filename),
        content_type=content_type,
        expiration=3600,
        client_method="put_object",
    )
    return response


def remove_uploaded_file(user_id: str, bot_id: str, filename: str):
    delete_file_from_s3(
        DOCUMENT_BUCKET, compose_upload_temp_s3_path(user_id, bot_id, filename)
    )
    return


def fetch_available_agent_tools():
    """Fetch available tools for bot."""
    return get_available_tools()


def fetch_guardrails():
    guardrails = list_guardrails()
    guardrails = filter(lambda x: x['status'] == 'READY', guardrails)
    results = []

    # for each guardrail, get all versions
    for guardrail in guardrails:
        logger.info(f"Getting versions for: {guardrail}")
        versioned_guardrails = list_guardrails(guardrail['id'])
        versions = [v['version'] for v in versioned_guardrails]
        sorted_versions = sorted(versions, reverse=True)
        logger.info(f"Found versions: {sorted_versions}")
        results.append(GuardrailDataOutput(
            id=guardrail['id'],
            name=guardrail['name'],
            versions=[v['version'] for v in list_guardrails(guardrail['id'])],
            default=guardrail['id'] == os.environ.get('DEFAULT_GUARDRAIL_ID', '')
        ))

    logger.info(f"Found guardrails: {results}")

    return GuardrailListOutput(guardrails=results)
