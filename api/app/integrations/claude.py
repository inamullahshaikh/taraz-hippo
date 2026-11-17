"""Blue on the server: Claude API, streamed, with a manual tool loop (max 20 tool calls per message)."""
import json

import anthropic
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from ..auth import Ctx
from ..db import Jsonb, tx
from ..services import blue_tools
from ..settings import S

MAX_TOOL_CALLS = 20
# $ per million tokens: input, output (cache reads bill at 10% of input, cache writes at 125%)
PRICES = {"claude-opus-5-5": (4, 20), "claude-sonnet-5-5": (2, 10), "claude-haiku-5-5": (0.1, 0.5), "claude-fable-5-1": (10, 50)}
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

SYSTEM = """You are Blue, the assistant inside BOMTree, Taraz's bill-of-materials app.
You help people read and work on BOMs: products and SFGs (semi-finished goods) grouped by family, each with BOM options
(e.g. BD/BC) and numbered versions that move Draft -> In review -> Approved -> Released -> Superseded.
Parts have Odoo codes (P0...); CMP marks a custom part, OTS a bought part.

Use the tools to look things up rather than guessing; quote item codes, part codes and ref des exactly.
You act with the rights of the person chatting. You can never approve, reject, release, push to Odoo, archive or manage users:
if asked, say a person has to press that button. Edits you prepare (propose_bulk_edit, match_import_lines suggestions,
draft_change_request) are proposals the person applies themselves: say so.
The page the person is on is given as context with each message. Keep answers short; use tables for lines."""


def cost_usd(model, usage):
    pin, pout = PRICES.get(model, (0, 0))
    return round((usage["input_tokens"] * pin + usage["cache_read_input_tokens"] * pin * 0.1
                  + usage["cache_creation_input_tokens"] * pin * 1.25 + usage["output_tokens"] * pout) / 1e6, 5)


def chat(user, roles, history, context):
    """Yields events: {'type': 'text'|'tool'|'proposal'|'file'|'done'|'error', ...}."""
    client = anthropic.Anthropic(api_key=S.ANTHROPIC_API_KEY)
    events = []
    messages = [{"role": m["role"], "content": m["content"]} for m in history[:-1]]
    messages.append({"role": "user", "content": f"{history[-1]['content']}\n\n<page_context>{json.dumps(context)}</page_context>"})
    extra = {"output_config": {"effort": "medium"}, "cache_control": {"type": "ephemeral"}}
    headers = {}
    if S.CLAUDE_MODEL in FALLBACK_MODELS:      # on a refusal the API retries on a fallback model itself
        extra["fallbacks"] = "default"
        headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
    usage = dict.fromkeys(("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"), 0)
    calls = 0
    try:
        while True:
            with client.messages.stream(model=S.CLAUDE_MODEL, max_tokens=16000, system=SYSTEM, tools=blue_tools.TOOLS,
                                        messages=messages, extra_body=extra, extra_headers=headers) as stream:
                for event in stream:
                    if event.type == "text":
                        yield {"type": "text", "text": event.text}
                response = stream.get_final_message()
            for k in usage:
                usage[k] += getattr(response.usage, k, 0) or 0
            if response.stop_reason == "refusal":
                yield {"type": "text", "text": "\n\nBlue can't help with that request."}
                break
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                break
            if response.stop_reason == "max_tokens":
                yield {"type": "text", "text": "\n\n(Stopped: the answer got too long.)"}
                break
            results = []
            for block in tool_uses:
                calls += 1
                yield {"type": "tool", "name": block.name}
                if calls > MAX_TOOL_CALLS:
                    results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                    "content": "Tool limit reached for this message. Answer with what you have."})
                    continue
                results.append(run_tool(user, roles, block, events))
                while events:
                    yield events.pop(0)
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": results})
    except anthropic.APIStatusError as e:
        yield {"type": "error", "message": f"Claude API error {e.status_code}"}
    except anthropic.APIConnectionError:
        yield {"type": "error", "message": "Could not reach the Claude API"}
    usd = cost_usd(S.CLAUDE_MODEL, usage)
    with tx(user["id"], "Blue") as conn:
        conn.execute("INSERT INTO audit_log (user_id, source, action, entity_type, after) VALUES (%s, 'Blue', 'blue chat', 'blue', %s)",
                     (user["id"], Jsonb({"model": S.CLAUDE_MODEL, "tool_calls": calls, "cost_usd": usd, **usage})))
    yield {"type": "done", "usage": usage, "cost_usd": usd}


def run_tool(user, roles, block, events):
    """One tool call = one database step, with the person's rights and source 'Blue'."""
    ok, out = True, None
    try:
        with tx(user["id"], "Blue") as conn:
            ctx = Ctx(conn, user, roles, source="Blue")
            out = blue_tools.run(ctx, block.name, block.input if isinstance(block.input, dict) else {}, events.append)
    except HTTPException as e:
        ok, out = False, e.detail if isinstance(e.detail, str) else e.detail.get("message", str(e.detail))
    except Exception as e:  # noqa: BLE001 - a broken tool becomes an error result, never a crashed chat
        ok, out = False, f"{type(e).__name__}: {e}"
    with tx(user["id"], "Blue") as conn:
        conn.execute("INSERT INTO audit_log (user_id, source, action, entity_type, after) VALUES (%s, 'Blue', %s, 'blue', %s)",
                     (user["id"], f"blue tool {block.name}", Jsonb(jsonable_encoder({"input": block.input, "ok": ok}))))
    content = json.dumps(jsonable_encoder(out), default=str)[:60000]
    return {"type": "tool_result", "tool_use_id": block.id, "content": content, **({} if ok else {"is_error": True})}
