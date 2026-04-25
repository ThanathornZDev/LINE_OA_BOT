from __future__ import annotations

import re


def _tlv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def _crc16_ccitt(data: str) -> str:
    crc = 0xFFFF
    for ch in data:
        crc ^= ord(ch) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return f"{crc:04X}"


def _format_promptpay_mobile(mobile_number: str) -> str:
    cleaned = re.sub(r"\D", "", mobile_number)
    if len(cleaned) != 10 or not cleaned.startswith("0"):
        raise ValueError("PROMPTPAY_ID mobile format must be 10 digits and start with 0")
    # Thailand country prefix for PromptPay mobile proxy
    return "0066" + cleaned[1:]


def _format_promptpay_target(target_id: str) -> tuple[str, str]:
    cleaned = re.sub(r"\D", "", target_id)
    if len(cleaned) == 10 and cleaned.startswith("0"):
        return "01", _format_promptpay_mobile(cleaned)
    if len(cleaned) == 13:
        return "02", cleaned
    raise ValueError("PROMPTPAY_ID must be Thai mobile (10 digits) or national ID (13 digits)")


def generate_promptpay_payload(target_id: str, amount: float | int | None = None) -> str:
    proxy_tag, proxy_value = _format_promptpay_target(target_id)

    merchant_account = _tlv("00", "A000000677010111") + _tlv(proxy_tag, proxy_value)

    payload = ""
    payload += _tlv("00", "01")
    payload += _tlv("01", "12")
    payload += _tlv("29", merchant_account)
    payload += _tlv("58", "TH")
    payload += _tlv("53", "764")

    if amount is not None:
        amount_value = f"{float(amount):.2f}"
        payload += _tlv("54", amount_value)

    payload_without_crc = payload + "6304"
    crc = _crc16_ccitt(payload_without_crc)
    return payload_without_crc + crc
