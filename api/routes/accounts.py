"""Accounts: cookie files, who they are, and the playlists they can see.

Cookie contents are never returned by the API, only uploaded."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func
from sqlmodel import Session, select

import core.accounts as accounts
from core.models import Source
from core.paths import ResolvedPaths

from ..convert import account_out
from ..deps import get_paths, get_session
from ..schemas import AccountCookiesUpdate, AccountCreate, AccountOut, AccountPlaylistOut

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _source_counts(session: Session) -> dict[str, int]:
    rows = session.exec(select(Source.account, func.count()).group_by(Source.account)).all()
    return {(acct or accounts.PRIMARY_ID): n for acct, n in rows}


def _require(paths: ResolvedPaths, account_id: str) -> accounts.Account:
    account = accounts.get_account(paths, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"No account '{account_id}'")
    return account


@router.get("", response_model=list[AccountOut])
def list_accounts(session: Session = Depends(get_session), paths: ResolvedPaths = Depends(get_paths)):
    counts = _source_counts(session)
    return [account_out(a, counts.get(a.id, 0)) for a in accounts.list_accounts(paths)]


@router.post("", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
def add_account(body: AccountCreate, paths: ResolvedPaths = Depends(get_paths)):
    """Add a secondary (read-only by default) account from an uploaded cookies.txt."""
    try:
        account = accounts.save_account(paths, label=body.label, cookies_text=body.cookies)
    except accounts.AccountError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return account_out(account)


@router.put("/{account_id}/cookies", response_model=AccountOut)
def replace_cookies(account_id: str, body: AccountCookiesUpdate, paths: ResolvedPaths = Depends(get_paths)):
    """Replace an account's cookies (they expire). Works for the primary too."""
    if account_id != accounts.PRIMARY_ID:
        _require(paths, account_id)
    try:
        account = accounts.save_account(
            paths, label=body.label or "", cookies_text=body.cookies, account_id=account_id
        )
    except accounts.AccountError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return account_out(account)


@router.post("/{account_id}/refresh", response_model=AccountOut)
def refresh_account(account_id: str, session: Session = Depends(get_session), paths: ResolvedPaths = Depends(get_paths)):
    """Re-check who this account is and whether its cookies still log in."""
    _require(paths, account_id)
    account = accounts.refresh_identity(paths, account_id)
    return account_out(account, _source_counts(session).get(account_id, 0))


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(account_id: str, session: Session = Depends(get_session), paths: ResolvedPaths = Depends(get_paths)):
    """Remove a secondary account. Sources that used it fall back to the primary."""
    try:
        accounts.delete_account(paths, account_id)
    except accounts.AccountError as exc:
        code = 404 if "No account" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc))
    for source in session.exec(select(Source).where(Source.account == account_id)).all():
        source.account = None
        session.add(source)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{account_id}/playlists", response_model=list[AccountPlaylistOut])
def account_playlists(account_id: str, session: Session = Depends(get_session), paths: ResolvedPaths = Depends(get_paths)):
    """Playlists this account can see (incl. private ones), each flagged with the
    id of the tracked source if it's already being archived. Hits YouTube."""
    account = _require(paths, account_id)
    try:
        playlists = accounts.list_playlists(account.cookies_file)
    except accounts.AccountError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    tracked = {url: sid for sid, url in session.exec(select(Source.id, Source.url)).all()}
    return [
        AccountPlaylistOut(
            playlist_id=p.playlist_id,
            title=p.title,
            url=p.url,
            video_count=p.video_count,
            source_id=tracked.get(p.url),
        )
        for p in playlists
    ]
