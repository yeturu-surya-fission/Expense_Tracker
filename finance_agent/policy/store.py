"""Companies and their policy versions (the only module that reads or writes them)."""
import secrets

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_agent.db.models import Company, PolicyVersion
from finance_agent.policy.models import Rule

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I: easy to read out loud


class PolicyError(ValueError):
    """A problem to tell the person in plain words."""


async def create_company(session: AsyncSession, name: str, manager_telegram_id: int) -> Company:
    if await company_for_manager(session, manager_telegram_id) is not None:
        raise PolicyError("You already have a company set up.")
    name = " ".join(name.split())[:120]
    if not name:
        raise PolicyError("Please give the company a name.")
    for _ in range(10):
        code = "FIN-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))
        if (await company_by_code(session, code)) is None:
            company = Company(code=code, name=name, manager_telegram_id=manager_telegram_id)
            session.add(company)
            await session.flush()
            return company
    raise PolicyError("Couldn't create a company code. Please try again.")


async def company_for_manager(session: AsyncSession, telegram_id: int) -> Company | None:
    return (await session.execute(select(Company).where(Company.manager_telegram_id == telegram_id))).scalar_one_or_none()


async def company_by_code(session: AsyncSession, code: str) -> Company | None:
    return (await session.execute(select(Company).where(Company.code == code.strip().upper()))).scalar_one_or_none()


async def current(session: AsyncSession, company_id: int) -> tuple[int, list[Rule]] | None:
    """(version, rules) of the latest saved policy, or None if the manager hasn't set one up."""
    row = (await session.execute(
        select(PolicyVersion).where(PolicyVersion.company_id == company_id).order_by(PolicyVersion.version.desc()).limit(1)
    )).scalar_one_or_none()
    return (row.version, [Rule.model_validate(r) for r in row.rules]) if row else None


async def save(session: AsyncSession, company_id: int, rules: list[Rule], note: str | None = None) -> int:
    """Save `rules` as the next version and return its number."""
    latest = (await session.execute(select(func.max(PolicyVersion.version)).where(PolicyVersion.company_id == company_id))).scalar() or 0
    session.add(PolicyVersion(company_id=company_id, version=latest + 1, rules=[r.model_dump(mode="json") for r in rules], note=(note or "")[:300] or None))
    await session.flush()
    return latest + 1


async def history(session: AsyncSession, company_id: int, limit: int = 10) -> list[PolicyVersion]:
    return list((await session.execute(
        select(PolicyVersion).where(PolicyVersion.company_id == company_id).order_by(PolicyVersion.version.desc()).limit(limit)
    )).scalars().all())
