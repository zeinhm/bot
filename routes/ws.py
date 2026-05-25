from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from bot import register_ws, unregister_ws

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    register_ws(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        unregister_ws(ws)
