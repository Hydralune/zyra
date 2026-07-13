from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class KeyboardCodecError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class KeyEventType(StrEnum):
    KEY_DOWN = "keyDown"
    KEY_UP = "keyUp"
    RAW_KEY_DOWN = "rawKeyDown"
    CHAR = "char"


@dataclass(frozen=True, slots=True)
class KeyDescriptor:
    key: str
    code: str
    windows_virtual_key_code: int
    native_virtual_key_code: int
    text: str = ""
    unmodified_text: str = ""
    location: int = 0
    is_keypad: bool = False

    def cdp_params(self, *, event_type: KeyEventType, modifiers: int = 0, auto_repeat: bool = False) -> dict[str, Any]:
        return {
            "type": str(event_type),
            "key": self.key,
            "code": self.code,
            "windowsVirtualKeyCode": self.windows_virtual_key_code,
            "nativeVirtualKeyCode": self.native_virtual_key_code,
            "text": self.text if event_type == KeyEventType.CHAR else "",
            "unmodifiedText": self.unmodified_text if event_type == KeyEventType.CHAR else "",
            "location": self.location,
            "isKeypad": self.is_keypad,
            "modifiers": modifiers,
            "autoRepeat": auto_repeat,
        }


@dataclass(frozen=True, slots=True)
class KeyChord:
    source: str
    descriptor: KeyDescriptor
    modifiers: int
    modifier_keys: tuple[KeyDescriptor, ...]

    @property
    def printable(self) -> bool:
        return bool(self.descriptor.text)

    def event_sequence(self) -> tuple[dict[str, Any], ...]:
        events: list[dict[str, Any]] = []
        active = 0
        for modifier in self.modifier_keys:
            active |= modifier_bit(modifier.key)
            events.append(modifier.cdp_params(event_type=KeyEventType.RAW_KEY_DOWN, modifiers=active))
        down_type = KeyEventType.KEY_DOWN if self.descriptor.text else KeyEventType.RAW_KEY_DOWN
        events.append(self.descriptor.cdp_params(event_type=down_type, modifiers=self.modifiers))
        if self.descriptor.text:
            events.append(self.descriptor.cdp_params(event_type=KeyEventType.CHAR, modifiers=self.modifiers))
        events.append(self.descriptor.cdp_params(event_type=KeyEventType.KEY_UP, modifiers=self.modifiers))
        for modifier in reversed(self.modifier_keys):
            active &= ~modifier_bit(modifier.key)
            events.append(modifier.cdp_params(event_type=KeyEventType.KEY_UP, modifiers=active))
        return tuple(events)


MODIFIER_BITS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}
MODIFIER_ALIASES = {
    "ALT": "Alt",
    "OPTION": "Alt",
    "CTRL": "Control",
    "CONTROL": "Control",
    "CMD": "Meta",
    "COMMAND": "Meta",
    "META": "Meta",
    "WIN": "Meta",
    "WINDOWS": "Meta",
    "SHIFT": "Shift",
}


SPECIAL_KEYS: dict[str, KeyDescriptor] = {
    "BACKSPACE": KeyDescriptor("Backspace", "Backspace", 8, 8),
    "TAB": KeyDescriptor("Tab", "Tab", 9, 9),
    "ENTER": KeyDescriptor("Enter", "Enter", 13, 13, "\r", "\r"),
    "RETURN": KeyDescriptor("Enter", "Enter", 13, 13, "\r", "\r"),
    "SHIFT": KeyDescriptor("Shift", "ShiftLeft", 16, 16, location=1),
    "CONTROL": KeyDescriptor("Control", "ControlLeft", 17, 17, location=1),
    "CTRL": KeyDescriptor("Control", "ControlLeft", 17, 17, location=1),
    "ALT": KeyDescriptor("Alt", "AltLeft", 18, 18, location=1),
    "PAUSE": KeyDescriptor("Pause", "Pause", 19, 19),
    "CAPSLOCK": KeyDescriptor("CapsLock", "CapsLock", 20, 20),
    "ESC": KeyDescriptor("Escape", "Escape", 27, 27),
    "ESCAPE": KeyDescriptor("Escape", "Escape", 27, 27),
    "SPACE": KeyDescriptor(" ", "Space", 32, 32, " ", " "),
    "PAGEUP": KeyDescriptor("PageUp", "PageUp", 33, 33),
    "PAGEDOWN": KeyDescriptor("PageDown", "PageDown", 34, 34),
    "END": KeyDescriptor("End", "End", 35, 35),
    "HOME": KeyDescriptor("Home", "Home", 36, 36),
    "ARROWLEFT": KeyDescriptor("ArrowLeft", "ArrowLeft", 37, 37),
    "LEFT": KeyDescriptor("ArrowLeft", "ArrowLeft", 37, 37),
    "ARROWUP": KeyDescriptor("ArrowUp", "ArrowUp", 38, 38),
    "UP": KeyDescriptor("ArrowUp", "ArrowUp", 38, 38),
    "ARROWRIGHT": KeyDescriptor("ArrowRight", "ArrowRight", 39, 39),
    "RIGHT": KeyDescriptor("ArrowRight", "ArrowRight", 39, 39),
    "ARROWDOWN": KeyDescriptor("ArrowDown", "ArrowDown", 40, 40),
    "DOWN": KeyDescriptor("ArrowDown", "ArrowDown", 40, 40),
    "INSERT": KeyDescriptor("Insert", "Insert", 45, 45),
    "DELETE": KeyDescriptor("Delete", "Delete", 46, 46),
    "META": KeyDescriptor("Meta", "MetaLeft", 91, 91, location=1),
    "CMD": KeyDescriptor("Meta", "MetaLeft", 91, 91, location=1),
    "CONTEXTMENU": KeyDescriptor("ContextMenu", "ContextMenu", 93, 93),
    "NUMLOCK": KeyDescriptor("NumLock", "NumLock", 144, 144, location=3, is_keypad=True),
    "SCROLLLOCK": KeyDescriptor("ScrollLock", "ScrollLock", 145, 145),
}
for function_number in range(1, 25):
    code = 111 + function_number
    SPECIAL_KEYS[f"F{function_number}"] = KeyDescriptor(f"F{function_number}", f"F{function_number}", code, code)


SHIFTED_DIGITS = {
    "1": "!",
    "2": "@",
    "3": "#",
    "4": "$",
    "5": "%",
    "6": "^",
    "7": "&",
    "8": "*",
    "9": "(",
    "0": ")",
}


PUNCTUATION: dict[str, tuple[str, int]] = {
    ";": ("Semicolon", 186),
    "=": ("Equal", 187),
    ",": ("Comma", 188),
    "-": ("Minus", 189),
    ".": ("Period", 190),
    "/": ("Slash", 191),
    "`": ("Backquote", 192),
    "[": ("BracketLeft", 219),
    "\\": ("Backslash", 220),
    "]": ("BracketRight", 221),
    "'": ("Quote", 222),
}


SHIFTED_PUNCTUATION = {
    ":": ";",
    "+": "=",
    "<": ",",
    "_": "-",
    ">": ".",
    "?": "/",
    "~": "`",
    "{": "[",
    "|": "\\",
    "}": "]",
    '"': "'",
}


def modifier_bit(key: str) -> int:
    return MODIFIER_BITS.get(key, 0)


def parse_key_chord(source: str) -> KeyChord:
    raw = str(source).strip()
    if not raw:
        raise KeyboardCodecError("key_chord_empty", "keyboard chord is empty")
    parts = split_chord(raw)
    if not parts:
        raise KeyboardCodecError("key_chord_empty", "keyboard chord is empty")
    modifiers: list[KeyDescriptor] = []
    modifier_value = 0
    for raw_modifier in parts[:-1]:
        name = MODIFIER_ALIASES.get(raw_modifier.upper())
        if name is None:
            raise KeyboardCodecError("unknown_modifier", f"unknown keyboard modifier {raw_modifier!r}")
        descriptor = SPECIAL_KEYS[name.upper() if name != "Control" else "CONTROL"]
        if descriptor.key in {item.key for item in modifiers}:
            continue
        modifiers.append(descriptor)
        modifier_value |= modifier_bit(descriptor.key)
    descriptor = key_descriptor(parts[-1], shift=bool(modifier_value & MODIFIER_BITS["Shift"]))
    return KeyChord(raw, descriptor, modifier_value, tuple(modifiers))


def split_chord(value: str) -> tuple[str, ...]:
    if value == "+":
        return ("+",)
    placeholder = "\x00PLUS\x00"
    escaped = value.replace("++", "+" + placeholder)
    parts = [item.strip().replace(placeholder, "+") for item in re.split(r"\s*\+\s*", escaped)]
    return tuple(item for item in parts if item)


def key_descriptor(value: str, *, shift: bool = False) -> KeyDescriptor:
    raw = str(value)
    upper = raw.upper().replace(" ", "")
    special = SPECIAL_KEYS.get(upper)
    if special is not None:
        return special
    if len(raw) != 1:
        raise KeyboardCodecError("unknown_key", f"unknown keyboard key {value!r}")
    if raw.isalpha() and raw.isascii():
        letter = raw.upper()
        text = letter if shift or raw.isupper() else letter.lower()
        code = "Key" + letter
        virtual = ord(letter)
        return KeyDescriptor(text, code, virtual, virtual, text, text.lower())
    if raw.isdigit():
        text = SHIFTED_DIGITS[raw] if shift else raw
        virtual = ord(raw)
        return KeyDescriptor(text, "Digit" + raw, virtual, virtual, text, raw)
    if raw in SHIFTED_PUNCTUATION:
        base = SHIFTED_PUNCTUATION[raw]
        code, virtual = PUNCTUATION[base]
        return KeyDescriptor(raw, code, virtual, virtual, raw, base)
    if raw in PUNCTUATION:
        code, virtual = PUNCTUATION[raw]
        return KeyDescriptor(raw, code, virtual, virtual, raw, raw)
    if raw == " ":
        return SPECIAL_KEYS["SPACE"]
    # Unicode text is inserted as a character event.  Virtual key codes remain
    # zero because Chromium accepts the exact text field for IME characters.
    return KeyDescriptor(raw, "", 0, 0, raw, raw)


def encode_key_sequence(values: str | Sequence[str]) -> tuple[dict[str, Any], ...]:
    selected = [values] if isinstance(values, str) else list(values)
    events: list[dict[str, Any]] = []
    for value in selected:
        events.extend(parse_key_chord(str(value)).event_sequence())
    return tuple(events)


def validate_key_sequence(values: str | Sequence[str], *, maximum_chords: int = 64) -> tuple[KeyChord, ...]:
    selected = [values] if isinstance(values, str) else list(values)
    if not selected or len(selected) > maximum_chords:
        raise KeyboardCodecError("key_sequence_size", "keyboard sequence size is outside policy limits")
    return tuple(parse_key_chord(str(value)) for value in selected)
