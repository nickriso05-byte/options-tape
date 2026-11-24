"""
The password in front of the app.

The password lives in the server's environment - APP_PASSWORD, set in Streamlit
Cloud's Secrets (which it also exposes as an environment variable) or in the
local, gitignored .streamlit/secrets.toml. It is never in the repository and
never sent to the browser: Streamlit runs this script on the server, so the
page only ever receives the login form, and what is typed is compared here.
Until the right password is entered, the script stops before anything else -
the scan, the live feed, the API calls - is built.

With no password configured the app stays locked rather than open, so a
deployment that is missing the secret cannot quietly expose everything.
"""

from __future__ import annotations

import hmac
import os
import time

import streamlit as st

_KEY = "tape_unlocked"


def _password() -> str | None:
    env = os.environ.get("APP_PASSWORD")
    if env:
        return env
    try:
        value = st.secrets.get("APP_PASSWORD")
        return str(value) if value else None
    except Exception:  # noqa: BLE001 - no secrets file
        return None


_COOKIE = "tape_auth"
_COOKIE_DAYS = 365


def _token(password: str) -> str:
    """What the browser keeps: a fingerprint of the password, never the password itself.
    Changing APP_PASSWORD changes the fingerprint, which signs every browser out."""
    return hmac.new(password.encode(), b"options-tape-login-v1", "sha256").hexdigest()


@st.cache_resource
def _cookie_setter():
    # A v2 component runs in the page itself (no iframe), so it can write the app's cookie directly.
    return st.components.v2.component("tape_login_cookie", js="""
export default function ({ data }) {
  if (data && data.cookie) { try { document.cookie = data.cookie; } catch (e) {} }
}""", isolate_styles=False)


def _remember(token: str) -> None:
    """Store the login in a first-party cookie for a year, so a reload or the app waking up doesn't ask again."""
    host = (st.context.headers.get("Host") or "").split(":")[0]
    secure = "" if host in ("localhost", "127.0.0.1") else "; Secure"
    _cookie_setter()(key="tape_cookie", data={"cookie": f"{_COOKIE}={token}; max-age={_COOKIE_DAYS * 86400}; path=/; SameSite=Lax{secure}"})


def require() -> None:
    """Show the login and stop the script until this browser is unlocked."""
    expected = _password()
    if expected:
        token = _token(expected)
        if hmac.compare_digest((st.context.cookies.get(_COOKIE) or ""), token):
            st.session_state[_KEY] = True
            return
        if st.session_state.get(_KEY):
            _remember(token)     # logged in this session but no cookie yet: set it
            return
    elif st.session_state.get(_KEY):
        return
    # Opened on this machine (localhost) with no password set: nothing to guard.
    host = (st.context.headers.get("Host") or "").split(":")[0]
    if not expected and host in ("localhost", "127.0.0.1"):
        return

    # The sidebar is built after this, so it would only show as an empty rail.
    st.markdown('<style>[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],'
                '[data-testid="stExpandSidebarButton"]{display:none !important}</style>',
                unsafe_allow_html=True)
    _, mid, _ = st.columns([1, 1.1, 1])
    with mid:
        st.markdown('<h2 style="text-align:center;margin:18vh 0 16px">Options Tape Lookup</h2>',
                    unsafe_allow_html=True)
        if not expected:
            st.markdown('<div class="gate-note">This app is locked. Set '
                        '<b>APP_PASSWORD</b> in the app’s secrets to open it.</div>',
                        unsafe_allow_html=True)
            st.stop()
        with st.form("gate", border=False):
            typed = st.text_input("Password", type="password",
                                  placeholder="Password", label_visibility="collapsed")
            submitted = st.form_submit_button("Enter", use_container_width=True)
        if submitted:
            # Constant-time, so the comparison's timing gives nothing away.
            if hmac.compare_digest(typed.encode(), expected.encode()):
                st.session_state[_KEY] = True
                st.rerun()
            time.sleep(1.0)              # slows guessing
            st.markdown('<div class="gate-note bad">That password isn’t right.</div>',
                        unsafe_allow_html=True)
    st.stop()
