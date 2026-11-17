import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..auth import CTX
from ..db import setting
from ..settings import S

router = APIRouter()


class Msg(BaseModel):
    role: str
    content: str


class ChatIn(BaseModel):
    messages: list[Msg]
    context: dict = {}


@router.post("/blue/chat")
def blue_chat(body: ChatIn, ctx=CTX):
    if not S.ANTHROPIC_API_KEY or not setting(ctx.conn, "blue_enabled"):
        raise HTTPException(409, "Blue is switched off")
    if not body.messages or body.messages[-1].role != "user":
        raise HTTPException(422, "The last message must be from the person")
    from ..integrations.claude import chat
    user, roles = dict(ctx.user), list(ctx.roles)
    history = [m.model_dump() for m in body.messages[-20:] if m.role in ("user", "assistant") and m.content]

    def stream():
        for event in chat(user, roles, history, body.context):
            yield f"data: {json.dumps(event, default=str)}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
