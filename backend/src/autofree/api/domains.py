"""域名池 API — 平铺管理。"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from autofree.db.base import get_db
from autofree.db.models import Domain
from autofree.deps import require_user

logger = logging.getLogger(__name__)
router = APIRouter()


class DomainCreateParams(BaseModel):
    domain: str = Field(min_length=1, max_length=255)

    @field_validator("domain")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip().lstrip("@").lower()


class DomainUpdateParams(BaseModel):
    enabled: Optional[bool] = None


def _serialize(d: Domain) -> dict:
    return {
        "id": d.id,
        "domain": d.domain,
        "enabled": d.enabled,
        "success_count": d.success_count,
        "fail_count": d.fail_count,
        "last_used_at": d.last_used_at.isoformat() if d.last_used_at else None,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


@router.get("")
def list_domains(db: Session = Depends(get_db), _user=Depends(require_user)) -> dict:
    rows = db.execute(select(Domain).order_by(Domain.id.desc())).scalars().all()
    return {"items": [_serialize(d) for d in rows]}


@router.post("", status_code=201)
def create_domain(
    params: DomainCreateParams,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> dict:
    existing = db.execute(select(Domain).where(Domain.domain == params.domain)).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"域名已存在: {params.domain}")
    d = Domain(domain=params.domain, enabled=True)
    db.add(d)
    db.commit()
    db.refresh(d)
    logger.info("[domains] 新增 %s id=%d", d.domain, d.id)
    return _serialize(d)


@router.patch("/{dom_id}")
def update_domain(
    dom_id: int,
    params: DomainUpdateParams,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
) -> dict:
    d = db.get(Domain, dom_id)
    if not d:
        raise HTTPException(404, "域名不存在")
    if params.enabled is not None:
        d.enabled = params.enabled
    db.commit()
    db.refresh(d)
    return _serialize(d)


@router.delete("/{dom_id}", status_code=204)
def delete_domain(
    dom_id: int, db: Session = Depends(get_db), _user=Depends(require_user)
) -> None:
    d = db.get(Domain, dom_id)
    if not d:
        raise HTTPException(404, "域名不存在")
    db.delete(d)
    db.commit()
    logger.info("[domains] 删除 id=%d %s", dom_id, d.domain)
