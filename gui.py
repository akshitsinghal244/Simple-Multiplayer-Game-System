"""
gui.py — In-game HUD for UDP Multiplayer Game

Components:
    TextInput   — keyboard-driven text buffer
    ChatLog     — scrolling message history
    Button      — clickable button with states
    HUD         — composes all components, owns layout

Usage in client.py:
    hud = HUD(screen_w, screen_h, on_connect=..., on_disconnect=...)
    hud.draw(screen)
    hud.handle_event(event)          # in your event loop
    hud.add_chat(name, message)      # when PKT_CHAT arrives
    hud.set_state("connected")       # "disconnected" | "connecting" | "connected"
"""

import pygame
from typing import Callable

# Palette
_C = {
    "panel":       (15,  17,  23,  210),   # near-black, semi-transparent
    "panel_line":  (40,  44,  55,  255),   # subtle border
    "text":        (220, 224, 232, 255),   # off-white
    "dim":         (100, 108, 120, 255),   # muted / placeholder
    "accent":      (0,   255, 170, 255),   # game's #00FFAA green
    "danger":      (255, 80,  80,  255),   # disconnect red
    "input_bg":    (22,  26,  34,  255),   # slightly lighter than panel
    "cursor":      (0,   255, 170, 180),   # accent, slightly transparent
    "btn_hover":   (30,  34,  44,  255),
    "btn_active":  (20,  24,  32,  255),
    "disabled":    (40,  44,  55,  255),
    "disabled_txt":(60,  66,  78,  255),
}

_FONT_MONO  = None   # loaded on first HUD init
_FONT_SMALL = None
_FONT_UI    = None

def _load_fonts():
    global _FONT_MONO, _FONT_SMALL, _FONT_UI
    if _FONT_MONO is not None:
        return
    # Prefer monospace for chat; fall back gracefully
    for name in ("Consolas", "Courier New", "DejaVu Sans Mono", None):
        try:
            _FONT_MONO  = pygame.font.SysFont(name, 14) if name else pygame.font.Font(None, 16)
            _FONT_SMALL = pygame.font.SysFont(name, 12) if name else pygame.font.Font(None, 14)
            break
        except Exception:
            continue
    for name in ("Segoe UI", "Arial", "Helvetica", None):
        try:
            _FONT_UI = pygame.font.SysFont(name, 13) if name else pygame.font.Font(None, 15)
            break
        except Exception:
            continue


# TextInput

class TextInput:
    """Single-line keyboard text buffer. Call update(event) per KEYDOWN event."""

    MAX_LEN = 100

    def __init__(self):
        self.text   = ""
        self.active = False
        self._cursor_tick  = 0
        self._cursor_visible = True

    def activate(self):
        self.active = True
        self.text   = ""
        self._cursor_tick = 0
        self._cursor_visible = True

    def deactivate(self):
        self.active = False

    def flush(self) -> str:
        """Return text and clear buffer."""
        out = self.text.strip()
        self.text = ""
        return out

    def handle_event(self, event: pygame.event.Event) -> bool:
        """Returns True if event was consumed."""
        if not self.active or event.type != pygame.KEYDOWN:
            return False
        if event.key == pygame.K_BACKSPACE:
            self.text = self.text[:-1]
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE):
            return False   # let HUD handle these
        elif len(self.text) < self.MAX_LEN:
            char = event.unicode
            if char and char.isprintable():
                self.text += char
        return True

    def tick(self, dt: float):
        """Call every frame with delta-time to animate cursor blink."""
        if not self.active:
            return
        self._cursor_tick += dt
        if self._cursor_tick >= 0.5:
            self._cursor_tick = 0.0
            self._cursor_visible = not self._cursor_visible

    def draw(self, surface: pygame.Surface, rect: pygame.Rect):
        """Draw the input field into rect."""
        pygame.draw.rect(surface, _C["input_bg"], rect, border_radius=3)
        pygame.draw.rect(surface, _C["accent"] if self.active else _C["panel_line"],
                         rect, 1, border_radius=3)

        pad = 6
        display = self.text + ("|" if self.active and self._cursor_visible else "")
        if not display and not self.active:
            surf = _FONT_MONO.render("Press T to chat…", True, _C["dim"])
        else:
            surf = _FONT_MONO.render(display, True, _C["text"])

        # Clip to box width
        clip = pygame.Rect(rect.x + pad, rect.y, rect.w - pad * 2, rect.h)
        surface.set_clip(clip)
        surface.blit(surf, (rect.x + pad, rect.y + (rect.h - surf.get_height()) // 2))
        surface.set_clip(None)


# ChatLog

class ChatLog:
    """Stores and renders the last N chat messages."""

    MAX_MESSAGES = 30
    DISPLAY_COUNT = 5     # lines visible at once
    LINE_H = 18
    FADE_AFTER = 8.0      # seconds before messages dim
    HIDE_AFTER  = 15.0    # seconds before messages disappear (when chat closed)

    def __init__(self):
        self._messages: list[dict] = []   # {name, text, t}
        self._now = 0.0

    def add(self, name: str, message: str, is_system: bool = False):
        self._messages.append({
            "name":      name,
            "text":      message,
            "t":         self._now,
            "system":    is_system,
        })
        if len(self._messages) > self.MAX_MESSAGES:
            self._messages.pop(0)

    def tick(self, dt: float):
        self._now += dt

    def draw(self, surface: pygame.Surface, rect: pygame.Rect, always_show: bool = False):
        """
        Draw the last DISPLAY_COUNT messages inside rect.
        always_show=True  → show regardless of age (chat is open).
        always_show=False → fade/hide old messages automatically.
        """
        visible = self._messages[-self.DISPLAY_COUNT:]
        y = rect.bottom - self.LINE_H

        for msg in reversed(visible):
            age = self._now - msg["t"]

            if not always_show:
                if age > self.HIDE_AFTER:
                    continue
                alpha = 255 if age < self.FADE_AFTER else int(
                    255 * (1 - (age - self.FADE_AFTER) / (self.HIDE_AFTER - self.FADE_AFTER))
                )
            else:
                alpha = 255

            if msg["system"]:
                line = f"  {msg['text']}"
                color = (*_C["accent"][:3], alpha)
            else:
                name_part = f"{msg['name']}: "
                text_part = msg["text"]

                # name in accent, text in normal
                name_surf = _FONT_MONO.render(name_part, True, (*_C["accent"][:3],))
                text_surf = _FONT_MONO.render(text_part, True, (*_C["text"][:3],))

                name_surf.set_alpha(alpha)
                text_surf.set_alpha(alpha)

                surface.blit(name_surf, (rect.x + 6, y))
                surface.blit(text_surf, (rect.x + 6 + name_surf.get_width(), y))
                y -= self.LINE_H
                continue

            surf = _FONT_MONO.render(line, True, color[:3])
            surf.set_alpha(alpha)
            surface.blit(surf, (rect.x + 6, y))
            y -= self.LINE_H


# Button

_BTN_CONNECT    = "connect"
_BTN_DISCONNECT = "disconnect"

class Button:
    """
    A single HUD button. States: normal | hover | pressed | disabled.
    Pass accent_color for the border/text highlight (defaults to accent green).
    """

    H = 26
    W = 90

    def __init__(self, label: str,
                 accent: tuple = None,
                 on_click: Callable = None):
        self.label    = label
        self.accent   = accent or _C["accent"]
        self.on_click = on_click
        self.disabled = False
        self._hover   = False
        self._pressed = False
        self.rect     = pygame.Rect(0, 0, self.W, self.H)

    def set_pos(self, x: int, y: int):
        self.rect.topleft = (x, y)

    def handle_event(self, event: pygame.event.Event):
        if self.disabled:
            return
        if event.type == pygame.MOUSEMOTION:
            self._hover = self.rect.collidepoint(event.pos)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if self.rect.collidepoint(event.pos):
                self._pressed = True
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            if self._pressed and self.rect.collidepoint(event.pos):
                if self.on_click:
                    self.on_click()
            self._pressed = False

    def draw(self, surface: pygame.Surface):
        if self.disabled:
            bg     = _C["disabled"]
            border = _C["panel_line"]
            tc     = _C["disabled_txt"]
        elif self._pressed:
            bg     = _C["btn_active"]
            border = self.accent
            tc     = self.accent
        elif self._hover:
            bg     = _C["btn_hover"]
            border = self.accent
            tc     = _C["text"]
        else:
            bg     = _C["panel"]
            border = _C["panel_line"]
            tc     = _C["dim"]

        pygame.draw.rect(surface, bg,     self.rect, border_radius=4)
        pygame.draw.rect(surface, border, self.rect, 1, border_radius=4)

        label_surf = _FONT_UI.render(self.label, True, tc)
        lx = self.rect.centerx - label_surf.get_width() // 2
        ly = self.rect.centery - label_surf.get_height() // 2
        surface.blit(label_surf, (lx, ly))


# HUD

class HUD:
    """
    Top-level HUD. Owns layout, routes events, exposes a clean API to client.py.

    Public API:
        hud.draw(screen)
        hud.handle_event(event)
        hud.tick(dt)
        hud.add_chat(name, message)
        hud.add_system(message)
        hud.set_state("disconnected" | "connecting" | "connected")
        hud.consume_chat() -> str | None   # returns message to send, if any
    """

    CHAT_H      = 120    # height of bottom chat panel
    BTN_PAD     = 8      # padding around top-right buttons
    PANEL_PAD   = 10

    def __init__(self, screen_w: int, screen_h: int,
                 on_connect: Callable    = None,
                 on_disconnect: Callable = None):

        _load_fonts()

        self.screen_w   = screen_w
        self.screen_h   = screen_h
        self._state     = "disconnected"
        self._chat_open = False
        self._pending   = None   # message waiting to be sent

        self._chat_log   = ChatLog()
        self._text_input = TextInput()

        self._btn_connect = Button(
            "Connect",
            accent   = _C["accent"],
            on_click = on_connect,
        )
        self._btn_disconnect = Button(
            "Disconnect",
            accent   = _C["danger"],
            on_click = on_disconnect,
        )

        self._layout()
        self.set_state("disconnected")

    # Layout

    def _layout(self):
        sw, sh = self.screen_w, self.screen_h
        pad = self.BTN_PAD

        # Top-right buttons
        self._btn_disconnect.set_pos(
            sw - Button.W - pad,
            pad,
        )
        self._btn_connect.set_pos(
            sw - Button.W * 2 - pad * 2,
            pad,
        )

        # Bottom chat panel rect
        self._chat_panel = pygame.Rect(
            0, sh - self.CHAT_H, sw, self.CHAT_H
        )

        # Chat log area (above input row)
        input_h = 28
        self._log_rect = pygame.Rect(
            self._chat_panel.x + self.PANEL_PAD,
            self._chat_panel.y + self.PANEL_PAD,
            sw - self.PANEL_PAD * 2,
            self.CHAT_H - input_h - self.PANEL_PAD * 3,
        )

        # Input row
        self._input_rect = pygame.Rect(
            self._chat_panel.x + self.PANEL_PAD,
            self._chat_panel.bottom - input_h - self.PANEL_PAD,
            sw - self.PANEL_PAD * 2,
            input_h,
        )

    # State

    def set_state(self, state: str):
        """'disconnected' | 'connecting' | 'connected'"""
        self._state = state
        if state == "disconnected":
            self._btn_connect.disabled    = False
            self._btn_disconnect.disabled = True
        elif state == "connecting":
            self._btn_connect.disabled    = True
            self._btn_disconnect.disabled = True
        elif state == "connected":
            self._btn_connect.disabled    = True
            self._btn_disconnect.disabled = False

    # ── Public API ────────────────────────────────────────────────────────────

    def add_chat(self, name: str, message: str):
        self._chat_log.add(name, message)

    def add_system(self, message: str):
        self._chat_log.add("", message, is_system=True)

    def consume_chat(self) -> "str | None":
        """Call once per frame. Returns a pending message string or None."""
        msg = self._pending
        self._pending = None
        return msg

    def is_chat_open(self) -> bool:
        return self._chat_open

    # Update

    def tick(self, dt: float):
        self._chat_log.tick(dt)
        self._text_input.tick(dt)

    def handle_event(self, event: pygame.event.Event):
        # Always route mouse events to buttons
        self._btn_connect.handle_event(event)
        self._btn_disconnect.handle_event(event)

        # Chat open/close
        if event.type == pygame.KEYDOWN:
            if not self._chat_open and event.key == pygame.K_t:
                self._open_chat()
                return

            if self._chat_open:
                if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    self._submit_chat()
                    return
                if event.key == pygame.K_ESCAPE:
                    self._close_chat()
                    return
                self._text_input.handle_event(event)

    def _open_chat(self):
        self._chat_open = True
        self._text_input.activate()

    def _close_chat(self):
        self._chat_open = False
        self._text_input.deactivate()

    def _submit_chat(self):
        msg = self._text_input.flush()
        if msg:
            self._pending = msg
        self._close_chat()

    # Draw

    def draw(self, screen: pygame.Surface):
        self._draw_chat_panel(screen)
        self._draw_top_bar(screen)

    def _draw_chat_panel(self, screen: pygame.Surface):
        # Only draw the panel bg when chat is open or recent messages exist
        if self._chat_open:
            panel_surf = pygame.Surface(
                (self._chat_panel.w, self._chat_panel.h), pygame.SRCALPHA
            )
            panel_surf.fill(_C["panel"])
            pygame.draw.line(
                panel_surf, _C["panel_line"],
                (0, 0), (self._chat_panel.w, 0), 1
            )
            screen.blit(panel_surf, self._chat_panel.topleft)

        self._chat_log.draw(screen, self._log_rect, always_show=self._chat_open)

        if self._chat_open:
            self._text_input.draw(screen, self._input_rect)

            # "T to chat / Esc to close" hint
            hint = "Enter ↵ send  •  Esc cancel"
            hint_surf = _FONT_SMALL.render(hint, True, _C["dim"])
            hx = self._input_rect.right - hint_surf.get_width() - 4
            hy = self._input_rect.bottom + 2
            screen.blit(hint_surf, (hx, hy))
        else:
            # Subtle "T" hint when chat is closed
            hint_surf = _FONT_SMALL.render("T  chat", True, _C["dim"])
            hint_surf.set_alpha(140)
            screen.blit(hint_surf, (self.PANEL_PAD, self._chat_panel.y + 4))

    def _draw_top_bar(self, screen: pygame.Surface):
        # Thin semi-transparent strip behind the buttons
        bar_h = Button.H + self.BTN_PAD * 2
        bar_surf = pygame.Surface((self.screen_w, bar_h), pygame.SRCALPHA)
        bar_surf.fill((15, 17, 23, 160))
        screen.blit(bar_surf, (0, 0))
        pygame.draw.line(screen, _C["panel_line"], (0, bar_h), (self.screen_w, bar_h), 1)

        # Status dot + label (left of top bar)
        dot_x, dot_y = 12, bar_h // 2
        if self._state == "connected":
            dot_color = _C["accent"]
            label     = "Online"
        elif self._state == "connecting":
            dot_color = (255, 200, 0, 255)
            label     = "Connecting…"
        else:
            dot_color = _C["danger"]
            label     = "Offline"

        pygame.draw.circle(screen, dot_color, (dot_x, dot_y), 4)
        lbl_surf = _FONT_UI.render(label, True, _C["dim"])
        screen.blit(lbl_surf, (dot_x + 10, dot_y - lbl_surf.get_height() // 2))

        self._btn_connect.draw(screen)
        self._btn_disconnect.draw(screen)