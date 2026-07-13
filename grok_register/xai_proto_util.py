"""最小 protobuf / gRPC-Web 编解码（仅注册流程所需字段）。"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote


def encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            break
    return bytes(out)


def encode_key(field: int, wire: int) -> bytes:
    return encode_varint((field << 3) | wire)


def encode_string(field: int, value: str) -> bytes:
    data = value.encode("utf-8")
    return encode_key(field, 2) + encode_varint(len(data)) + data


def encode_bytes(field: int, data: bytes) -> bytes:
    return encode_key(field, 2) + encode_varint(len(data)) + data


def encode_varint_field(field: int, value: int) -> bytes:
    return encode_key(field, 0) + encode_varint(value)


def encode_message(field: int, message: bytes) -> bytes:
    return encode_bytes(field, message)


def grpc_web_frame(message: bytes) -> bytes:
    """gRPC-Web / gRPC 数据帧: 1 byte flags + 4 byte big-endian length + payload."""
    return b"\x00" + struct.pack(">I", len(message)) + message


def parse_grpc_web_response(content: bytes, headers: Dict) -> Tuple[int, str, bytes]:
    """返回 (grpc_status, grpc_message, message_bytes)。"""
    status = headers.get("grpc-status")
    message = headers.get("grpc-message") or headers.get("Grpc-Message") or ""
    if status is not None:
        try:
            st = int(status)
        except ValueError:
            st = 2
        return st, unquote(str(message)), content

    # trailers may be in body (grpc-web)
    msg_data = b""
    trailer_text = ""
    i = 0
    data = content or b""
    while i + 5 <= len(data):
        flags = data[i]
        length = struct.unpack(">I", data[i + 1 : i + 5])[0]
        i += 5
        chunk = data[i : i + length]
        i += length
        if flags & 0x80:  # trailer
            trailer_text = chunk.decode("utf-8", errors="replace")
        else:
            msg_data = chunk

    st = 0
    msg = ""
    for line in trailer_text.split("\r\n"):
        if line.lower().startswith("grpc-status:"):
            try:
                st = int(line.split(":", 1)[1].strip())
            except ValueError:
                st = 2
        if line.lower().startswith("grpc-message:"):
            msg = unquote(line.split(":", 1)[1].strip())

    # some servers only put status in trailer frame without message frame
    if not trailer_text and not msg_data and data:
        # raw trailer-only sometimes
        text = data.decode("utf-8", errors="replace")
        if "grpc-status" in text:
            for line in text.replace("\n", "\r\n").split("\r\n"):
                if line.lower().startswith("grpc-status:"):
                    try:
                        st = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                if line.lower().startswith("grpc-message:"):
                    msg = unquote(line.split(":", 1)[1].strip())

    return st, msg, msg_data


def decode_varint(buf: bytes, i: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
    raise ValueError("truncated varint")


def decode_fields(buf: bytes) -> List[Tuple[int, int, bytes]]:
    """粗解析 protobuf 字段列表 (field, wire, raw_value_bytes/payload)."""
    out: List[Tuple[int, int, bytes]] = []
    i = 0
    while i < len(buf):
        key, i = decode_varint(buf, i)
        field = key >> 3
        wire = key & 7
        if wire == 0:
            val, i = decode_varint(buf, i)
            out.append((field, wire, encode_varint(val)))
        elif wire == 1:
            out.append((field, wire, buf[i : i + 8]))
            i += 8
        elif wire == 2:
            length, i = decode_varint(buf, i)
            out.append((field, wire, buf[i : i + length]))
            i += length
        elif wire == 5:
            out.append((field, wire, buf[i : i + 4]))
            i += 4
        else:
            break
    return out


# ---- request builders (auth_mgmt.proto) ----

def build_create_email_validation_code(email: str, castle_token: str = "") -> bytes:
    """CreateEmailValidationCodeRequest: email=1, castle_request_token=3."""
    msg = encode_string(1, email)
    if castle_token:
        msg += encode_string(3, castle_token)
    return msg


def build_verify_email_validation_code(
    email: str,
    code: str,
    delete_on_success: bool = False,
) -> bytes:
    """VerifyEmailValidationCodeRequest: email=1, email_validation_code=2, delete_on_success=3."""
    msg = encode_string(1, email) + encode_string(2, code)
    if delete_on_success:
        msg += encode_varint_field(3, 1)
    return msg


def build_anti_abuse_token(turnstile_token: str) -> bytes:
    """AntiAbuseToken: turnstile_token=1."""
    return encode_string(1, turnstile_token)


def build_create_user_request(
    email: str,
    password: str,
    given_name: str,
    family_name: str,
    tos_accepted_version: int = 1,
    turnstile_token: str = "",
) -> bytes:
    """CreateUserRequest fields: given_name=1 family_name=2 email=3 clear_text_password=5 tos=6 anti_abuse=7."""
    msg = (
        encode_string(1, given_name)
        + encode_string(2, family_name)
        + encode_string(3, email)
        + encode_string(5, password)
        + encode_varint_field(6, tos_accepted_version)
    )
    if turnstile_token:
        msg += encode_message(7, build_anti_abuse_token(turnstile_token))
    return msg


def build_create_user_and_session(
    email: str,
    password: str,
    given_name: str,
    family_name: str,
    email_validation_code: str,
    turnstile_token: str = "",
    castle_token: str = "",
    tos_accepted_version: int = 1,
    num_one_time_links: int = 1,
) -> bytes:
    """CreateUserAndSessionRequest.

    create_user_request=1
    anti_abuse_token=6
    num_one_time_links=7
    email_validation_code=9
    castle_request_token=11
    """
    user = build_create_user_request(
        email=email,
        password=password,
        given_name=given_name,
        family_name=family_name,
        tos_accepted_version=tos_accepted_version,
        turnstile_token=turnstile_token,
    )
    msg = encode_message(1, user)
    if turnstile_token:
        msg += encode_message(6, build_anti_abuse_token(turnstile_token))
    if num_one_time_links:
        msg += encode_varint_field(7, int(num_one_time_links))
    msg += encode_string(9, email_validation_code)
    if castle_token:
        msg += encode_string(11, castle_token)
    return msg


def build_create_session_email_password(
    email: str,
    password: str,
    turnstile_token: str = "",
    tos_version: int = 1,
    num_one_time_links: int = 1,
) -> bytes:
    """CreateSessionRequest with EmailAndPassword credentials.

    credentials=1 (oneof):
      email_and_password = field 1 inside Credentials message
        -> actually Credentials is a oneof container; from descriptor:
           EmailAndPasswordRequest is nested; field numbers for oneof
           typically: email_and_password = 1 on Credentials
    anti_abuse_token=4
    num_one_time_links=5
    tos_version=6
    """
    email_pwd = encode_string(1, email) + encode_string(2, password)
    # Credentials.email_and_password = field 1 (message)
    credentials = encode_message(1, email_pwd)
    msg = encode_message(1, credentials)
    if turnstile_token:
        msg += encode_message(4, build_anti_abuse_token(turnstile_token))
    if num_one_time_links:
        msg += encode_varint_field(5, int(num_one_time_links))
    if tos_version:
        msg += encode_varint_field(6, int(tos_version))
    return msg


def collect_string_fields(buf: bytes) -> List[str]:
    """递归收集 protobuf 中所有 wire=2 的字符串字段。"""
    out: List[str] = []

    def walk(data: bytes) -> None:
        try:
            fields = decode_fields(data)
        except Exception:
            return
        for _field, wire, payload in fields:
            if wire != 2:
                continue
            # try as utf-8 string
            try:
                s = payload.decode("utf-8")
                if s.isprintable() or any(c in s for c in ("@", "-", "_")):
                    out.append(s)
            except Exception:
                pass
            # also recurse as nested message
            if len(payload) >= 2:
                walk(payload)

    walk(buf or b"")
    return out


def collect_uuid_like(buf: bytes) -> List[str]:
    import re as _re

    uu = _re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )
    return [s for s in collect_string_fields(buf) if uu.match(s)]
