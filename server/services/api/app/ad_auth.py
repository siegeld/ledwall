"""LDAP/Active Directory authentication helper.

Shared VERBATIM between the consoles it came from, which is the point: an auth
helper that has been copied and then diverged is an auth helper with two
different bugs. Reads config from the `settings` table (admin-editable in the
UI), not from env vars — so AD settings can change at runtime.

Two paths:
  - Direct user bind (default): the user's password authenticates the LDAP bind.
    Supports UPN form (`user@domain.com`) when the user typed their email at the
    login form — AD routes across domains in the forest. NTLM form (`DOMAIN\\user`)
    is the fallback when only a bare username is provided.
  - Search-then-bind (when `ad_bind_dn` is set): a service account searches for
    the user, then re-binds as them to verify the password.

Multi-domain forest support:
  - Set `ad_global_catalog` to a GC URL (port 3268). User-lookup searches run
    against the GC with an empty base, spanning every domain in the forest.
    Without a GC, search is limited to the partition `ad_server` holds.
  - Bind always happens against `ad_server` — UPN-based binds work against any DC
    in the forest because AD routes the auth.

Admin = membership in `ad_admin_group`. For SYSTEM consoles, callers must treat a
non-admin AD user as UNAUTHORIZED (see routers/auth.py) — a successful bind is
necessary but not sufficient.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ldap3 import ALL_ATTRIBUTES, SUBTREE, Connection, Server
from ldap3.core.exceptions import LDAPException

log = logging.getLogger(__name__)


def _ldap_escape(value: str) -> str:
    """Escape LDAP filter special chars per RFC 4515."""
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\5c")
        elif ch == "*":
            out.append("\\2a")
        elif ch == "(":
            out.append("\\28")
        elif ch == ")":
            out.append("\\29")
        elif ch == "\x00":
            out.append("\\00")
        else:
            out.append(ch)
    return "".join(out)


def _is_admin_via_member_of(member_of: list[str], admin_group: str) -> bool:
    """Match either a full DN (CN=Domain Admins,CN=Users,...) or a short CN
    (Domain Admins) in the user's memberOf attribute."""
    if not admin_group or not member_of:
        return False
    if admin_group in member_of:
        return True
    target = admin_group.lower()
    for dn in member_of:
        low = dn.lower()
        if low.startswith("cn="):
            cn = low.split(",", 1)[0][3:]
            if cn == target:
                return True
    return False


def _ad_authenticate_blocking(
    *,
    server_url: str,
    gc_url: str,
    base_search: str,
    bind_dn: str,
    bind_password: str,
    domain: str,
    admin_group: str,
    username: str,
    upn: str | None,
    password: str,
) -> dict[str, Any] | None:
    """Synchronous LDAP bind + lookup. Returns user info dict on success, None on
    failure. Caller wraps in asyncio.to_thread."""
    try:
        bind_server = Server(server_url, get_info="ALL")
        search_server = Server(gc_url, get_info="ALL") if gc_url else bind_server

        if bind_dn:
            # Service-account search-then-bind.
            search_conn_user = bind_dn
            search_conn_pass = bind_password
        else:
            # Direct bind AS THE USER. Prefer UPN (forest-wide); fall back to NTLM.
            if upn:
                search_conn_user = upn
            elif domain:
                search_conn_user = f"{domain}\\{username}"
            else:
                search_conn_user = username
            search_conn_pass = password

        try:
            conn = Connection(
                search_server,
                user=search_conn_user, password=search_conn_pass,
                auto_bind=True, read_only=True,
            )
        except LDAPException as e:
            log.warning("AD: bind failed for %s on %s as %r: %s",
                        username, gc_url or server_url, search_conn_user, e)
            return None

        # Match by sAMAccountName OR userPrincipalName.
        filter_parts = [f"(sAMAccountName={_ldap_escape(username)})"]
        if upn:
            filter_parts.append(f"(userPrincipalName={_ldap_escape(upn)})")
        search_filter = (
            f"(|{''.join(filter_parts)})" if len(filter_parts) > 1 else filter_parts[0]
        )

        effective_base = "" if gc_url else base_search  # empty base = forest-wide on GC
        conn.search(
            search_base=effective_base,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=ALL_ATTRIBUTES,
        )
        if not conn.entries:
            log.warning("AD: user not found via %s (base=%r, filter=%s); "
                        "configure ad_global_catalog for forest-wide lookup",
                        gc_url or server_url, effective_base, search_filter)
            conn.unbind()
            return None

        entry = conn.entries[0]
        user_dn = str(entry.entry_dn)

        # Service-account path: verify the user's password by binding as them.
        if bind_dn:
            conn.unbind()
            user_principal = upn or user_dn
            try:
                user_conn = Connection(
                    bind_server, user=user_principal, password=password,
                    auto_bind=True, read_only=True,
                )
            except LDAPException as e:
                log.warning("AD: user-credential bind failed for %s as %r: %s",
                            username, user_principal, e)
                return None
            user_conn.unbind()

        display_name = str(entry.displayName) if hasattr(entry, "displayName") else username
        email = str(entry.mail) if hasattr(entry, "mail") else None
        member_of = [str(g) for g in entry.memberOf] if hasattr(entry, "memberOf") else []
        is_admin = _is_admin_via_member_of(member_of, admin_group)
        conn.unbind()
        return {
            "username": username,
            "dn": user_dn,
            "display_name": display_name,
            "email": email,
            "is_admin": is_admin,
        }
    except LDAPException as e:
        log.warning("AD: authentication failed for %s: %s", username, e)
        return None
    except Exception:
        log.exception("AD: unexpected error for %s", username)
        return None


async def ad_authenticate(
    settings_dict: dict[str, Any],
    *,
    username: str,
    upn: str | None = None,
    password: str,
) -> dict[str, Any] | None:
    """Async wrapper. Returns dict with username/dn/display_name/email/is_admin on
    success, None on failure (including disabled AD)."""
    if not settings_dict.get("ad_enabled"):
        return None
    server = (settings_dict.get("ad_server") or "").strip()
    if not server:
        return None
    return await asyncio.to_thread(
        _ad_authenticate_blocking,
        server_url=server,
        gc_url=(settings_dict.get("ad_global_catalog") or "").strip(),
        base_search=(settings_dict.get("ad_user_search_base")
                     or settings_dict.get("ad_base_dn") or ""),
        bind_dn=(settings_dict.get("ad_bind_dn") or "").strip(),
        bind_password=(settings_dict.get("ad_bind_password") or ""),
        domain=(settings_dict.get("ad_domain") or "").strip(),
        admin_group=(settings_dict.get("ad_admin_group") or "").strip(),
        username=username,
        upn=upn,
        password=password,
    )
