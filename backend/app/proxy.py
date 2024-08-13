import asyncio
import websockets
import json
import logging

logging.basicConfig(level=logging.INFO)

DOCKER_WEBSOCKET_URL = "ws://localhost:9000/dev"

async def proxy_handler(websocket, path):
    connection_id = str(id(websocket))

    try:

        async with websockets.connect(DOCKER_WEBSOCKET_URL) as docker_ws:

            connect_event = {
                "requestContext": {
                    "routeKey": "$connect",
                    "connectionId": connection_id,
                    "domainName": "localhost",
                    "stage": "dev"
                }
            }
            logging.info(f'Sending connect event to Docker WebSocket server: {connect_event}')
            await docker_ws.send(json.dumps(connect_event))

            try:
                async for message in websocket:
                    logging.info(f'Message coming to proxy WebSocket: {message}')

                    data = json.loads(message)
                    forward_event = {
                        "requestContext": {
                            "routeKey": "$default",
                            "connectionId": connection_id,
                            "domainName": "localhost",
                            "stage": "dev"
                        },
                        "body": json.dumps(data)
                    }
                    logging.info(f'Forwarding event to Docker WebSocket server: {forward_event}')
                    await docker_ws.send(json.dumps(forward_event))


                    response = await docker_ws.recv()
                    logging.info(f'Response from Docker WebSocket server: {response}')
                    await websocket.send(response)
            finally:

                disconnect_event = {
                    "requestContext": {
                        "routeKey": "$disconnect",
                        "connectionId": connection_id,
                        "domainName": "localhost",
                        "stage": "dev"
                    }
                }
                logging.info(f'Sending disconnect event to Docker WebSocket server: {disconnect_event}')
                await docker_ws.send(json.dumps(disconnect_event))
    except Exception as e:
        logging.error(f'Error handling WebSocket connection: {e}')

async def main():
    server = await websockets.serve(proxy_handler, "localhost", 8001)
    logging.info("WebSocket proxy server started on ws://localhost:8001")
    await server.wait_closed()

if __name__ == "__main__":
    asyncio.run(main())
