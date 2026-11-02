"""
Tool registration. A tool is a plain async function `fn(call, ...)` decorated with `@tool(...)`. When the server starts,
`register_all` wraps each one so that EVERY call, whoever makes it, goes through the same gate:

    verified token -> scope check -> chat-context check -> rate limit -> idempotency -> run -> audit -> structured result

The wrapper adds three optional arguments to every user tool (clients that are AI agents hide them from the model):
    answers          {question key: chosen option id} - the human's answers after an `input_required` result
    idempotency_key  repeating a successful write with the same key returns the first result instead of writing again
    member_count     advisory: how many people the chat platform says are in the group chat
"""
import inspect
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable, Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mcp_server.config import settings
from mcp_server.db.models import AuditLog, IdempotencyRecord
from mcp_server.db.session import session_scope
from mcp_server.domain.errors import UserError
from mcp_server.tools._common import Call, NeedInput, Out

log = logging.getLogger(__name__)

HIDDEN_DOC = (
    "\n\nProtocol arguments (agents: do not invent these): `answers` = the human's answers to an earlier input_required "
    "result, as {key: option id}; `idempotency_key` = a unique id per intended action, so a retry never writes twice; "
    "`member_count` = people in the group chat according to the chat platform."
)


class ToolResult(BaseModel):
    """What every tool returns. `status` tells the client what to do next."""

    status: Literal["ok", "input_required", "rejected", "error"] = Field(
        description="ok: done. input_required: ask the human the questions in input_requests, then call the SAME tool again "
                    "with the same arguments plus answers. rejected: a normal 'no' (bad input, unknown friend...), show `text`. "
                    "error: the call could not be processed."
    )
    text: str = Field(default="", description="Ready-to-show message for the human.")
    data: dict[str, Any] = Field(default_factory=dict, description="Structured facts (ids for undo buttons, people who must press Start...).")
    input_requests: list[dict[str, Any]] = Field(default_factory=list, description="Questions to put to the human (status=input_required).")
    error: dict[str, str] | None = Field(default=None, description="{code, message} when status=error.")


@dataclass
class ToolSpec:
    name: str
    fn: Callable
    scope: str  # user | platform
    contexts: frozenset[str]  # chat contexts it works in: private, group
    write: bool


SPECS: dict[str, ToolSpec] = {}


def tool(*, scope: str = "user", contexts: tuple[str, ...] = ("private", "group"), write: bool = False):
    def deco(fn):
        SPECS[fn.__name__] = ToolSpec(fn.__name__, fn, scope, frozenset(contexts), write)
        return fn
    return deco


# ------------------------------------------------------------------ rate limit (per caller, sliding one-minute window)
_hits: dict[str, deque] = defaultdict(deque)


def rate_limited(subject: str, limit: int | None = None) -> bool:
    limit = limit or settings.rate_limit_per_minute
    now, q = time.monotonic(), _hits[subject]
    if len(_hits) > 10_000:  # forget callers that have been quiet for a minute (keeps memory bounded)
        for key in [k for k, v in _hits.items() if not v or now - v[-1] > 60]:
            del _hits[key]
        q = _hits[subject]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


def reset_rate_limits() -> None:
    _hits.clear()


# ------------------------------------------------------------------ audit + idempotency
async def audit(subject: str, actor: str | None, action: str, outcome: str, detail: str | None = None) -> None:
    try:
        async with session_scope() as s:
            s.add(AuditLog(subject=subject[:64], actor=(actor or None) and actor[:64], action=action[:64], outcome=outcome, detail=(detail or None) and detail[:200]))
    except Exception:  # an audit failure must never break the call
        log.exception("audit write failed")


async def _stored_result(user_id: int, key: str) -> dict | None:
    async with session_scope() as s:
        return (await s.execute(select(IdempotencyRecord.result).where(IdempotencyRecord.user_id == user_id, IdempotencyRecord.key == key))).scalar_one_or_none()


async def _store_result(user_id: int, key: str, tool_name: str, result: dict) -> None:
    async with session_scope() as s:
        await s.execute(
            pg_insert(IdempotencyRecord).values(user_id=user_id, key=key, tool=tool_name, result=result).on_conflict_do_nothing(constraint="uq_idem_user_key")
        )


# ------------------------------------------------------------------ the gate
def _result(status: str, text: str = "", data: dict | None = None, input_requests: list | None = None, error: dict | None = None) -> dict:
    return ToolResult(status=status, text=text, data=data or {}, input_requests=input_requests or [], error=error).model_dump()


def _hidden_params() -> list[inspect.Parameter]:
    kw = inspect.Parameter.POSITIONAL_OR_KEYWORD
    return [
        inspect.Parameter("answers", kw, default=None, annotation=dict[str, str] | None),
        inspect.Parameter("idempotency_key", kw, default=None, annotation=str | None),
        inspect.Parameter("member_count", kw, default=None, annotation=int | None),
    ]


def build_wrapper(spec: ToolSpec) -> Callable:
    sig = inspect.signature(spec.fn)
    own = list(sig.parameters.values())[1:]  # drop `call`
    user_tool = spec.scope == "user"
    params = own + (_hidden_params() if user_tool else [])

    async def wrapper(**kwargs):
        token = get_access_token()
        if token is None:
            return _result("error", error={"code": "unauthenticated", "message": "No valid access token."})
        user_tool = spec.scope == "user"
        claims = token.claims or {}
        granted = set(token.scopes or [])
        subject = token.subject or "unknown"
        actor = (claims.get("act") or {}).get("sub") if isinstance(claims.get("act"), dict) else claims.get("act")
        if spec.scope not in granted:
            await audit(subject, actor, spec.name, "denied", f"needs scope {spec.scope}")
            return _result("error", error={"code": "insufficient_scope", "message": f"This tool needs the '{spec.scope}' scope."})
        # a platform client serves many people at once (outbox polling, acknowledgements): its ceiling is much higher
        if rate_limited(subject, None if user_tool else settings.rate_limit_per_minute * 20):
            await audit(subject, actor, spec.name, "rate_limited")
            return _result("error", error={"code": "rate_limited", "message": "Too many requests. Please slow down."})

        if user_tool:
            answers, idem, member_count = kwargs.pop("answers", None), kwargs.pop("idempotency_key", None), kwargs.pop("member_count", None)
            chat = claims.get("chat_type", "private")
            if chat not in spec.contexts:
                await audit(subject, actor, spec.name, "denied", f"not in {chat} chat")
                return _result("error", error={"code": "not_available_here", "message": f"{spec.name} is not available in a {chat} chat."})
            if claims.get("uid") is None:
                return _result("error", error={"code": "no_user", "message": "This tool needs a user session token."})
            call: Any = Call(
                user_id=int(claims["uid"]), subject=subject, actor=actor, chat_type=chat,
                group_chat_id=claims.get("group_chat_id"), answers=dict(answers or {}),
                member_count=member_count, tool=spec.name,
            )
            if idem and spec.write:
                done = await _stored_result(call.user_id, idem[:100])
                if done is not None:
                    await audit(subject, actor, spec.name, "ok", "replayed")
                    return done
        else:
            call = PlatformCall(client_id=subject, tool=spec.name)

        try:
            out = await spec.fn(call, **kwargs)
            if isinstance(out, Out):
                result = _result("ok", out.text, out.data)
            else:
                text = str(out)
                result = _result("rejected" if text.startswith("❌") else "ok", text)
            outcome = result["status"]
        except NeedInput as need:
            result, outcome = _result("input_required", need.question, input_requests=[need.as_request()]), "input_required"
        except UserError as e:
            result, outcome = _result("rejected", f"❌ {e}"), "ok"
        except Exception as e:
            log.exception("tool %s failed", spec.name)
            result, outcome = _result("error", error={"code": "internal", "message": f"{type(e).__name__}"}), "error"

        if user_tool and idem and spec.write and result["status"] == "ok":
            await _store_result(call.user_id, idem[:100], spec.name, result)
        await audit(subject, actor, spec.name, outcome)
        return result

    wrapper.__name__ = spec.name
    wrapper.__qualname__ = spec.name
    wrapper.__doc__ = (spec.fn.__doc__ or spec.name).strip() + (HIDDEN_DOC if user_tool else "")
    wrapper.__signature__ = inspect.Signature(params, return_annotation=ToolResult)
    wrapper.__annotations__ = {p.name: p.annotation for p in params} | {"return": ToolResult}
    return wrapper


@dataclass
class PlatformCall:
    client_id: str
    tool: str


def register_all(mcp) -> None:
    for spec in SPECS.values():
        mcp.tool(name=spec.name)(build_wrapper(spec))
