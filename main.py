#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradeTime (4시간봉 전용, 단일 박스)
- 큰 숫자는 실제 한국(서울) 시간을 실시간으로 표시
- 세그먼트 바 4칸으로 4시간봉이 얼마나 진행됐는지 표시
- 항상 위에 표시, 크기/투명도/효과음 조절, mac 알림센터, 효과음, 테두리 점멸
- 매 1시간 구간이 채워지기 5분 전 알림
- 크기/투명도/효과음 설정은 다음에 켤 때도 그대로 기억됩니다

실행:
    pip install -r requirements.txt
    python main.py

박스를 마우스로 드래그해서 원하는 위치로 옮길 수 있습니다.
메뉴바(트레이) 아이콘을 클릭하면 크기, 투명도, 효과음을 조절할 수 있습니다.
"""

import sys
import os
import json
import re
import base64
import time
import subprocess
import threading
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ_NAME = "Asia/Seoul"
KST = ZoneInfo(DEFAULT_TZ_NAME)  # 유지: 4시간봉 계산 등 내부 기본값 참고용
US_EASTERN = ZoneInfo("America/New_York")  # 미국 경제지표 발표시각(ET) 기준, DST 자동 반영

CONFIG_PATH = os.path.expanduser("~/.bitcoin_candle_clock_config.json")

from PySide6.QtCore import Qt, QTimer, QRectF, QObject, Signal
from PySide6.QtGui import QPainter, QColor, QFont, QFontMetrics, QPainterPath, QIcon, QPixmap, QAction, QActionGroup, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QSystemTrayIcon, QMenu, QInputDialog, QMessageBox,
    QDialog, QHBoxLayout, QLabel, QPushButton, QWidgetAction, QFrame
)

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")  # HH:MM (24시간제) 검증용

# ----------------------------------------------------------------------
# 설정값
# ----------------------------------------------------------------------
BASE_WIDTH = 340
OUTER_MARGIN = 14

SEGMENT_COUNT = 4          # 4시간봉 = 1시간짜리 세그먼트 4칸 (항상 UTC 기준, 시간대 선택과 무관)
BLINK_DURATION_SECONDS = 10.0   # 알림 시 테두리가 깜빡이는 총 시간(초)
BLINK_INTERVAL_MS = 500         # 깜빡임 토글 주기(ms)

ALERT_VOLUME = 2.6         # 알림 효과음 볼륨 (1.0=기본, 총 네 단계 정도 더 크게)
SOUND_REPEAT = 3           # 알림 효과음 연속 재생 횟수

MAX_CUSTOM_ALARMS = 10     # 사용자가 추가할 수 있는 알람 최대 개수

MIN_SCALE, MAX_SCALE, SCALE_STEP = 0.6, 2.2, 0.1
MIN_OPACITY, MAX_OPACITY, OPACITY_STEP = 0.3, 1.0, 0.1

PINK_COLOR = QColor(0xFF, 0x21, 0xFF)      # #ff21ff (기본 알림 색)
BG_COLOR = QColor(54, 54, 54)              # 박스 배경 (10% 더 어둡게 조정)
SEG_ON_COLOR = QColor(255, 255, 255)       # 채워진 세그먼트
SEG_OFF_COLOR = QColor(140, 140, 140)      # 미채워진 세그먼트
LABEL_COLOR = QColor(0, 174, 239)          # 상단 라벨(하늘색, 현재 미사용)
TEXT_COLOR = QColor(255, 255, 255)         # 시계 기본 글자색 (흰색)

# 알림(테두리 점멸 + 정각 텍스트) 색상 선택 목록. Off=None은 시각 효과 끄기(소리/알림은 그대로)
ALERT_COLOR_CHOICES = [
    ("Off", None),
    ("Pink", "#ff21ff"),
    ("Red", "#ff0044"),
    ("Blue", "#00ffff"),
    ("Green", "#31fd2e"),
    ("Yellow", "#fefd48"),
    ("White", "#ffffff"),
]
DEFAULT_ALERT_COLOR_HEX = "#ff21ff"

# 리마인더(정각 몇 분 전에 알릴지) 선택 목록. 0=Off(끄기)
REMINDER_PRESETS = [
    ("Off", 0),
    ("5 min before", 5),
    ("10 min before", 10),
    ("15 min before", 15),
]
DEFAULT_REMINDER_MINUTES = 5

# ----------------------------------------------------------------------
# Market Events (경제지표 카운트다운) - FRED(세인트루이스 연준) 공개 API 사용
# ----------------------------------------------------------------------
# 키는 소스코드에 직접 넣지 않고 환경변수에서 읽어옴 (README의 "Setting up API keys" 참고).
# https://fred.stlouisfed.org/docs/api/api_key.html 에서 무료로 발급받을 수 있음.
# 등록 안 해도 앱 자체는 정상 실행되고, Market Events 기능만 꺼진 채로 동작함.
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_BASE = "https://api.stlouisfed.org/fred"
ECON_RELEASE_HOUR_ET = 8             # 발표 시각(미국 동부시간 기준) - 대부분 08:30 ET
ECON_RELEASE_MINUTE_ET = 30
ECON_REFRESH_INTERVAL_SEC = 6 * 3600  # 일정 재확인 주기(초). 하루 4번이면 충분히 최신 유지됨

# 월 약자는 시스템 로케일(strftime의 %b)에 의존하면 한글 등으로 나와 폰트에서 깨질 수 있으므로
# 항상 고정된 영어 3글자 약자를 직접 씀
MONTH_ABBR_EN = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

# (표시코드, FRED release_id, FRED series_id, units 파라미터, 표시형식)
ECONOMIC_EVENTS_DEF = [
    ("CPI",            10,  "CPIAUCSL",  "pc1",  "pct_signed"),
    ("Core CPI",       10,  "CPILFESL",  "pc1",  "pct_signed"),
    ("PPI",            46,  "PPIFIS",    "pc1",  "pct_signed"),
    ("Core PPI",       46,  "PPIFES",    "pc1",  "pct_signed"),
    ("NFP",            50,  "PAYEMS",    "chg",  "thousands_signed"),
    ("Unemployment",   50,  "UNRATE",    "lin",  "pct_plain"),
    ("GDP",            53,  "GDPC1",     "pca",  "pct_signed"),
    ("Retail Sales",   9,   "RSAFS",     "pch",  "pct_signed"),
    ("Jobless Claims", 180, "ICSA",      "lin",  "level_plain"),
    ("PCE",            54,  "PCEPI",     "pc1",  "pct_signed"),
    ("Core PCE",       54,  "PCEPILFE",  "pc1",  "pct_signed"),
]


def fmt_econ_value(value, fmt):
    if fmt == "pct_signed":
        return f"{value:+.1f}%"
    elif fmt == "pct_plain":
        return f"{value:.1f}%"
    elif fmt == "thousands_signed":
        return f"{value:+,.0f}K"
    elif fmt == "level_plain":
        return f"{value:,.0f}"
    return f"{value}"


# ----------------------------------------------------------------------
# Breaking News (키워드 기반 속보 티커) - Finnhub 뉴스 API 사용
# ----------------------------------------------------------------------
# 키는 소스코드에 직접 넣지 않고 환경변수에서 읽어옴 (README의 "Setting up API keys" 참고).
# https://finnhub.io/register 에서 무료로 발급받을 수 있음 (무료 플랜: 분당 60건, 초당 30건).
# 등록 안 해도 앱 자체는 정상 실행되고, Breaking News 기능만 꺼진 채로 동작함.
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
FINNHUB_BASE = "https://finnhub.io/api/v1"

# 조회할 뉴스 카테고리: general(일반 시장/거시 뉴스) + crypto(코인 전용 뉴스)
# forex/merger는 지금 키워드 목적(나스닥 선물/코인 변동 요인)과 겹치는 게 적어서 제외
NEWS_CATEGORIES = ["general", "crypto"]

# 폴링 주기: 무료 플랜(분당 60건) 기준, 카테고리 2개(요청 2회/폴링)를 감안해도
# 10초 간격이면 분당 12건(한도의 20%)만 써서 안전 마진이 넉넉함
NEWS_POLL_INTERVAL_SEC = 10
NEWS_SEEN_HISTORY_MAX = 300        # 중복 노출 방지용으로 기억해두는 기사 id 최대 개수

# 로테이션 시간창: 최근 이 시간(초) 이내에 키워드 매칭된 뉴스만 화면에서 순환 노출됨.
# 이보다 오래된 건 자동으로 빠짐 -> "몇 시간 전 뉴스가 계속 나옴" 방지.
# 10분: '속보'라 부를 만한 신선도 기준 + 조용한 시간대에도 순환시킬 최소 분량 확보의 절충값
NEWS_ROTATION_WINDOW_SEC = 10 * 60

NEWS_SCROLL_TICK_MS = 33           # 마퀴 스크롤 갱신 주기(약 30fps)
NEWS_SCROLL_SPEED_PX = 1.4         # scale=1 기준, 프레임당 스크롤 이동량(px)
NEWS_SCROLL_GAP_PX = 60            # 같은 텍스트가 한 바퀴 끝나고 다음이 시작되기 전 여백

# 나스닥 선물/코인 시세에 영향을 줄만한 키워드 (타이틀에 포함된 경우만 속보로 인정)
BREAKING_NEWS_KEYWORDS = [
    # 거시/금리
    "Fed", "FOMC", "Powell", "Rate Cut", "Rate Hike", "Interest Rate", "Inflation",
    "CPI", "PPI", "NFP", "Nonfarm Payrolls", "GDP", "PCE", "Core PCE", "Retail Sales",
    "PMI", "Jobless Claims", "Treasury Yield", "10-Year", "2-Year", "Recession",
    # 정치/지정학
    "Trump", "Tariff", "War", "China", "Taiwan", "Sanctions", "Israel", "Iran",
    # 코인/크립토
    "BTC", "Bitcoin", "Ethereum", "Ripple", "XRP", "Solana", "Circle", "Tether",
    "MicroStrategy", "Halving", "Spot ETF", "Stablecoin", "Grayscale", "FTX",
    "Liquidation", "Hack", "Exploit", "Bankruptcy",
    # 거래소/기관/규제
    "SEC", "ETF", "BlackRock", "Coinbase", "Binance", "Kraken",
    # 나스닥 메가캡
    "Nasdaq", "Nvidia", "Apple", "Tesla", "Microsoft", "Amazon",
    # 원자재
    "Oil",
]

CLOCK_SAMPLE_TEXT = "00:00:00"  # 폰트 크기를 고정으로 계산할 때 쓰는 기준 문자열

SOUND_PATH_MAIN = "/System/Library/Sounds/Glass.aiff"
SOUND_PATH_SUB = "/System/Library/Sounds/Ping.aiff"

# 효과음 선택 메뉴에서 고를 수 있는 macOS 기본 사운드 목록 (이름, 파일경로). Off=None은 무음
SOUND_CHOICES = [
    ("Off", None),
    ("Glass (Default)", "/System/Library/Sounds/Glass.aiff"),
    ("Ping", "/System/Library/Sounds/Ping.aiff"),
    ("Sosumi", "/System/Library/Sounds/Sosumi.aiff"),
    ("Hero", "/System/Library/Sounds/Hero.aiff"),
    ("Pop", "/System/Library/Sounds/Pop.aiff"),
    ("Tink", "/System/Library/Sounds/Tink.aiff"),
    ("Funk", "/System/Library/Sounds/Funk.aiff"),
    ("Basso", "/System/Library/Sounds/Basso.aiff"),
]

# 시계 숫자에 표시할 도시/시간대 선택 목록 (이름, IANA 시간대명)
# 주의: 상단 세그먼트 바(4시간봉 진행률)는 이 선택과 무관하게 항상 UTC 기준으로 계산됨
TIMEZONE_CHOICES = [
    ("Seoul (KST)", "Asia/Seoul"),
    ("Tokyo (JST)", "Asia/Tokyo"),
    ("Shanghai (CST)", "Asia/Shanghai"),
    ("Singapore (SGT)", "Asia/Singapore"),
    ("Hong Kong (HKT)", "Asia/Hong_Kong"),
    ("India (IST)", "Asia/Kolkata"),
    ("Dubai (GST)", "Asia/Dubai"),
    ("London (UK)", "Europe/London"),
    ("Frankfurt (CET)", "Europe/Berlin"),
    ("New York (ET)", "America/New_York"),
    ("Chicago (CT)", "America/Chicago"),
    ("Los Angeles (PT)", "America/Los_Angeles"),
    ("UTC", "UTC"),
]

# 시계 숫자 서체 굵기 선택 목록 (이름, 굵기값 1-1000 스케일). SF Pro 계열 명명 그대로 사용
FONT_WEIGHT_CHOICES = [
    ("Ultralight", 100),
    ("Thin", 200),
    ("Light", 300),
    ("Regular", 400),
    ("Medium", 500),   # 기본값
    ("Semibold", 600),
    ("Bold", 700),
    ("Heavy", 800),
    ("Black", 900),
]
DEFAULT_FONT_WEIGHT = 500


def play_sound(path, volume=1.0, repeat=1):
    if not path:  # Off로 선택된 경우(None)
        return
    try:
        if repeat <= 1:
            subprocess.Popen(["afplay", "-v", str(volume), path])
        else:
            # 짧은 간격을 두고 순서대로 여러 번 재생 (셸에서 백그라운드로 실행되므로 앱은 멈추지 않음)
            safe_path = path.replace('"', '\\"')
            cmd = f'for i in $(seq 1 {int(repeat)}); do afplay -v {volume} "{safe_path}"; sleep 0.15; done'
            subprocess.Popen(["bash", "-c", cmd])
    except Exception:
        pass


def bring_app_to_front():
    """팝업(알람 추가 등)을 열기 직전에 호출하면, 크롬 등 다른 앱 뒤에 가려지지 않고
    이 프로그램이 macOS에서 최전면으로 올라온다."""
    try:
        pid = os.getpid()
        script = f'tell application "System Events" to set frontmost of (first process whose unix id is {pid}) to true'
        subprocess.run(["osascript", "-e", script], timeout=2)
    except Exception:
        pass


def send_mac_notification(title, message, sound="Glass"):
    t = title.replace('"', '\\"')
    m = message.replace('"', '\\"')
    script = f'display notification "{m}" with title "{t}" sound name "{sound}"'
    try:
        subprocess.Popen(["osascript", "-e", script])
    except Exception:
        pass


def fmt_clock(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


def load_config():
    """마지막으로 저장된 설정(크기/투명도/효과음)을 불러온다. 파일이 없거나 깨졌으면 기본값 사용."""
    defaults = {
        "scale": 1.0,
        "opacity": 0.9,
        "alert_sound": SOUND_PATH_MAIN,
        "timezone": DEFAULT_TZ_NAME,
        "custom_alarms": [],
        "font_weight": DEFAULT_FONT_WEIGHT,
        "alert_color": DEFAULT_ALERT_COLOR_HEX,
        "reminder_minutes": DEFAULT_REMINDER_MINUTES,
        "market_events_enabled": False,
        "breaking_news_enabled": False,
    }
    try:
        with open(CONFIG_PATH, "r") as f:
            data = json.load(f)
        for key in defaults:
            if key in data:
                defaults[key] = data[key]
    except Exception:
        pass
    return defaults


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f)
    except Exception:
        pass


# ----------------------------------------------------------------------
# 캔들 박스 위젯 (4시간봉, 현재시각 표시)
# ----------------------------------------------------------------------
class CandleBox(QWidget):
    INNER_PAD_BASE = 16     # 박스 안쪽 여백 (상하좌우 동일하게 사용)
    BAR_H_BASE = 8
    BAR_GAP_BASE = 6
    TEXT_GAP_BASE = 3.9     # 진행바와 시계 사이 간격 (기존값에서 30% 추가 축소)

    def __init__(self, label, parent=None):
        super().__init__(parent)
        self.label = label

        self.now_kst = datetime.now(KST)  # 화면에 표시되는 실제 한국시간
        self.filled_segments = 0          # 완전히 채워진 세그먼트(=완료된 시간) 개수
        self.partial_fraction = 0.0       # 현재 진행중인 세그먼트의 진행률(0~1)

        self.text_pink_active = False   # 매시 정각 5분 전부터 5분간 시계 숫자를 (계속) 알림색으로
        self.text_blink_with_border = False  # 사용자 지정 알람: 테두리와 함께 시계 숫자도 깜빡깜빡
        self.border_alert_active = False
        self.border_blink_on = False
        self.alert_color = QColor(DEFAULT_ALERT_COLOR_HEX)  # None이면 시각 효과 꺼짐(Off)

        self.scale = 1.0
        self.font_weight = DEFAULT_FONT_WEIGHT
        # 레이아웃 캐시 (scale이 바뀔 때만 재계산 -> 매초 글자 크기가 흔들리지 않음)
        self._inner_pad = self.INNER_PAD_BASE
        self._bottom_pad = self.INNER_PAD_BASE * 0.7
        self._bar_h = self.BAR_H_BASE
        self._bar_gap = self.BAR_GAP_BASE
        self._text_gap = self.TEXT_GAP_BASE
        self._font_size = 10

        self._recompute_layout()

    def _recompute_layout(self):
        """scale/font_weight가 바뀔 때만 호출. 폰트 크기를 고정 기준 문자열로 한 번만 계산해서
        캐시해두므로, 실제 시각이 매초 바뀌어도 폰트 크기는 절대 흔들리지 않는다."""
        s = self.scale
        box_w = int(BASE_WIDTH * s)
        inner_pad = self.INNER_PAD_BASE * s
        bar_h = self.BAR_H_BASE * s
        bar_gap = self.BAR_GAP_BASE * s
        text_gap = self.TEXT_GAP_BASE * s

        target_w = box_w - 2 * inner_pad

        font_size = 10
        best_size = font_size
        while font_size < 400:
            trial_font = QFont("SF Pro Rounded", font_size)
            trial_font.setWeight(QFont.Weight(self.font_weight))
            fm = QFontMetrics(trial_font)
            if fm.horizontalAdvance(CLOCK_SAMPLE_TEXT) > target_w:
                break
            best_size = font_size
            font_size += 1

        # 박스 폭에 맞는 최대 크기를 구한 뒤, 사용자가 원하는 만큼(+2pt) 더 키움 (잘림 방지 위해 여유를 둠)
        best_size = best_size + 2

        final_font = QFont("SF Pro Rounded", best_size)
        final_font.setWeight(QFont.Weight(self.font_weight))
        fm = QFontMetrics(final_font)
        text_h = fm.height()  # AlignVCenter가 실제로 쓰는 표준 라인 높이 기준 (상하 여백을 정확히 맞추기 위함)

        # 상/좌/우 여백은 inner_pad로 균일하게, 아래쪽만 30% 더 좁게
        bottom_pad = inner_pad * 0.7
        content_h = inner_pad + bar_h + text_gap + text_h + bottom_pad

        self._inner_pad = inner_pad
        self._bottom_pad = bottom_pad
        self._bar_h = bar_h
        self._bar_gap = bar_gap
        self._text_gap = text_gap
        self._font_size = best_size

        self.setFixedHeight(int(round(content_h)))

    def set_scale(self, scale):
        self.scale = scale
        self._recompute_layout()
        self.update()

    def set_font_weight(self, weight):
        self.font_weight = weight
        self._recompute_layout()
        self.update()

    def update_time(self, now_kst: datetime, filled_segments: int, partial_fraction: float):
        self.now_kst = now_kst
        self.filled_segments = filled_segments
        self.partial_fraction = partial_fraction
        self.update()

    def set_text_pink(self, active: bool):
        self.text_pink_active = active
        self.update()

    def set_alert_color(self, color_or_none):
        self.alert_color = color_or_none
        self.update()

    def start_border_blink(self, blink_text: bool = False):
        self.border_alert_active = True
        self.text_blink_with_border = blink_text
        self.update()

    def stop_border_blink(self):
        self.border_alert_active = False
        self.border_blink_on = False
        self.text_blink_with_border = False
        self.update()

    def toggle_border_blink(self):
        if self.border_alert_active:
            self.border_blink_on = not self.border_blink_on
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            self._paint(painter)
        except Exception as e:
            print(f"[그리기 오류] {self.label}: {e}")
        finally:
            painter.end()

    def _paint(self, painter):
        s = self.scale
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        radius = 8 * s   # 스크린샷 디자인에 맞춘 작은 라운드

        # ---- 박스 배경 (박스가 위젯 전체를 채움) ----
        path = QPainterPath()
        path.addRoundedRect(0, 0, w, h, radius, radius)
        painter.fillPath(path, BG_COLOR)

        # 알림 중일 때 테두리 점멸 (색상은 사용자가 고른 Alert Color, Off면 표시 안 함)
        if self.border_alert_active and self.border_blink_on and self.alert_color is not None:
            pen = painter.pen()
            pen.setColor(self.alert_color)
            pen.setWidth(max(2, int(4 * s)))
            painter.setPen(pen)
            painter.drawPath(path)

        # ---- 세그먼트 진행 바 (좌우 여백 = inner_pad, 균일한 여백용) ----
        inner_pad = self._inner_pad
        bar_top = inner_pad
        bar_h = self._bar_h
        seg_gap = self._bar_gap
        bar_total_w = w - 2 * inner_pad
        seg_w = (bar_total_w - seg_gap * (SEGMENT_COUNT - 1)) / SEGMENT_COUNT

        for i in range(SEGMENT_COUNT):
            seg_x = inner_pad + i * (seg_w + seg_gap)
            seg_path = QPainterPath()
            seg_path.addRoundedRect(seg_x, bar_top, seg_w, bar_h, bar_h / 2, bar_h / 2)

            if i < self.filled_segments:
                painter.fillPath(seg_path, SEG_ON_COLOR)
            elif i == self.filled_segments and self.partial_fraction > 0:
                painter.fillPath(seg_path, SEG_OFF_COLOR)
                painter.save()
                painter.setClipRect(QRectF(seg_x, bar_top, seg_w * self.partial_fraction, bar_h))
                painter.fillPath(seg_path, SEG_ON_COLOR)
                painter.restore()
            else:
                painter.fillPath(seg_path, SEG_OFF_COLOR)

        # ---- 현재 시각 (한국시간, KST) : 폰트 크기는 scale이 바뀔 때만 재계산된 고정값 사용 ----
        text = fmt_clock(self.now_kst)
        digit_font = QFont("SF Pro Rounded", self._font_size)
        digit_font.setWeight(QFont.Weight(self.font_weight))
        digit_font.setLetterSpacing(QFont.PercentageSpacing, 102)
        painter.setFont(digit_font)
        if self.alert_color is None:
            text_color = TEXT_COLOR
        elif self.border_alert_active and self.text_blink_with_border:
            # 점멸 중: 시계 숫자가 테두리와 함께 토글 (알림색 <-> 흰색)
            text_color = self.alert_color if self.border_blink_on else TEXT_COLOR
        elif self.text_pink_active:
            # 점멸이 끝난 뒤에도 (매시 정각 알림처럼) 알림색을 유지해야 하는 경우
            text_color = self.alert_color
        else:
            text_color = TEXT_COLOR
        painter.setPen(text_color)

        text_rect_top = bar_top + bar_h + self._text_gap
        text_rect_h = h - text_rect_top - self._bottom_pad
        # 가운데 정렬 대신 좌측 정렬: 초마다 글자 폭이 미세하게 달라져도
        # 숫자 시작 위치가 고정되어 시계가 좌우로 흔들리지 않음
        painter.drawText(
            int(inner_pad), int(text_rect_top), int(w - inner_pad), int(text_rect_h),
            Qt.AlignLeft | Qt.AlignVCenter, text
        )


# ----------------------------------------------------------------------
# 메인 윈도우
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# 경제지표 이벤트 하나의 상태
# ----------------------------------------------------------------------
class EconomicEvent:
    def __init__(self, code, release_id, series_id, units, fmt):
        self.code = code
        self.release_id = release_id
        self.series_id = series_id
        self.units = units
        self.fmt = fmt
        self.target_utc = None       # 다음 발표 예정 시각 (UTC, datetime) - 모르면 None
        self.state = "loading"       # loading(일정 조회중) / counting(대기중)


# ----------------------------------------------------------------------
# Market Events 매니저: FRED API로 발표 일정/결과를 가져와 관리
#   - 켜져 있을 때만 주기적으로 조회 (꺼져있으면 네트워크 요청 자체를 안 함)
#   - 모든 네트워크 호출은 백그라운드 스레드에서 (UI 멈춤 방지)
# ----------------------------------------------------------------------
class MarketEventsManager(QObject):
    changed = Signal()

    def __init__(self, api_key):
        super().__init__()
        self.api_key = api_key
        self.enabled = False
        self.events = [EconomicEvent(*d) for d in ECONOMIC_EVENTS_DEF]
        self._lock = threading.Lock()
        self._last_refresh = 0.0
        self._warned_no_key = False

    def set_enabled(self, enabled):
        was_enabled = self.enabled
        self.enabled = enabled
        if enabled and not was_enabled:
            self._refresh_all_dates()
        self.changed.emit()

    def _refresh_all_dates(self):
        if not self.api_key:
            if not self._warned_no_key:
                self._warned_no_key = True
                print("[Market Events] FRED_API_KEY가 설정되지 않았습니다. "
                      "환경변수 FRED_API_KEY를 발급받은 키로 설정하세요 (README 참고).")
            return
        self._last_refresh = time.monotonic()
        for ev in self.events:
            threading.Thread(target=self._fetch_next_date, args=(ev,), daemon=True).start()

    def _fetch_next_date(self, ev):
        try:
            today = datetime.now(timezone.utc).date().isoformat()
            params = {
                "release_id": ev.release_id,
                "realtime_start": today,
                "realtime_end": "9999-12-31",  # FRED 관례상 "미래 끝없음" - 이게 없으면 기본값이 "오늘까지"라 미래 일정이 안 잡힘
                "include_release_dates_with_no_data": "true",  # 미래 예정일은 당연히 아직 데이터가 없으므로 true여야 함
                "sort_order": "asc",
                "limit": "1",
                "file_type": "json",
                "api_key": self.api_key,
            }
            url = f"{FRED_BASE}/release/dates?" + urllib.parse.urlencode(params)
            with urllib.request.urlopen(url, timeout=8) as resp:
                data = json.loads(resp.read().decode())
            dates = data.get("release_dates", [])
            if not dates:
                print(f"[Market Events] {ev.code}: 다음 발표일 정보를 찾지 못했습니다 (응답에 날짜 없음)")
                return
            y, m, d = map(int, dates[0]["date"].split("-"))
            local_dt = datetime(y, m, d, ECON_RELEASE_HOUR_ET, ECON_RELEASE_MINUTE_ET, tzinfo=US_EASTERN)
            target_utc = local_dt.astimezone(timezone.utc)
            with self._lock:
                ev.target_utc = target_utc
                ev.state = "counting"
            print(f"[Market Events] {ev.code}: 다음 발표일 확인됨 -> {target_utc.isoformat()} (UTC)")
        except Exception as e:
            print(f"[Market Events 오류] {ev.code} 일정 조회 실패: {e}")
        self.changed.emit()

    def tick(self):
        """1초마다 ClockWindow.tick()에서 호출됨. 꺼져있으면 아무 일도 안 함(요청 없음)."""
        if not self.enabled:
            return

        # 주기적으로(기본 6시간마다) 전체 일정을 다시 확인해 최신 상태 유지
        if time.monotonic() - self._last_refresh > ECON_REFRESH_INTERVAL_SEC:
            self._refresh_all_dates()

        now = datetime.now(timezone.utc)
        any_changed = False
        with self._lock:
            for ev in self.events:
                # 카운트다운이 끝나면 결과 노출 없이 곧바로 다음 발표일을 다시 조회해서 넘어감
                if ev.state == "counting" and ev.target_utc and now >= ev.target_utc:
                    ev.state = "loading"
                    ev.target_utc = None
                    threading.Thread(target=self._fetch_next_date, args=(ev,), daemon=True).start()
                    any_changed = True
        if any_changed:
            self.changed.emit()

    def get_display_lines(self, display_tz):
        """(코드, 종류, 시각문자열_또는_None, 값문자열) 튜플 리스트.
        종류: 'countdown' / 'preview'
        - 발표까지 12시간 이내로 다가온 이벤트는 카운트다운으로 보여줌
        - 12시간 이내가 아니면, 가장 가까운 다음 발표 '날짜'와 같은 날짜에 발표되는
          이벤트들을 모두 날짜+시각(미리보기)으로 보여줌 (같은 날 2~3개면 각각 한 줄씩)
        - 카운트다운이 끝나면 결과 노출 없이 바로 다음 이벤트 일정으로 전환됨"""
        if not self.enabled:
            return []

        now = datetime.now(timezone.utc)
        lines = []

        with self._lock:
            counting = [
                ev for ev in self.events
                if ev.state == "counting" and ev.target_utc and (ev.target_utc - now).total_seconds() > 0
            ]

            if not counting:
                return lines

            nearest_time = min(ev.target_utc for ev in counting)
            nearest_date = nearest_time.astimezone(display_tz).date()
            # 가장 가까운 발표일과 '동일한 날짜'에 발표되는 이벤트들을 전부 모음 (2~3개면 각각 한 줄)
            same_day = sorted(
                (ev for ev in counting if ev.target_utc.astimezone(display_tz).date() == nearest_date),
                key=lambda e: e.target_utc,
            )

            for ev in same_day:
                remaining = ev.target_utc - now
                local_time = ev.target_utc.astimezone(display_tz)
                if remaining <= timedelta(hours=12):
                    total = int(remaining.total_seconds())
                    hh, rem = divmod(total, 3600)
                    mm, ss = divmod(rem, 60)
                    countdown = f"{hh:02d}:{mm:02d}:{ss:02d}"
                    lines.append((ev.code, "countdown", local_time.strftime("%H:%M"), countdown))
                else:
                    date_str = f"{local_time.day} {MONTH_ABBR_EN[local_time.month - 1]} {local_time.year}. {local_time.strftime('%H:%M')}"
                    lines.append((ev.code, "preview", None, date_str))

        return lines


# ----------------------------------------------------------------------
# Market Events 표시줄 (박스 바로 아래, 시계 박스와 동일한 스타일의 프레임을
# 이벤트 줄 수(1~3줄)에 딱 맞게 타이트하게 그려서 보여줌)
# ----------------------------------------------------------------------
class MarketEventsPanel(QWidget):
    LINE_HEIGHT_BASE = 20   # 줄 하나의 높이 (scale=1 기준)
    PAD_V_BASE = 8          # 프레임 위/아래 안쪽 여백 (scale=1 기준) - 타이트하게
    PAD_H_BASE = 14         # 프레임 좌/우 안쪽 여백 (scale=1 기준)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lines = []
        self.scale = 1.0
        self.setFixedHeight(0)

    def set_scale(self, scale):
        """CandleBox와 같은 scale을 공유해서, 프레임의 라운드/여백이 시계 박스와 항상 같은 비율로 보이게 함."""
        self.scale = scale
        self._recompute_height()
        self.update()

    def has_content(self):
        return bool(self.lines)

    def update_lines(self, lines):
        if lines == self.lines:
            return
        self.lines = lines
        self._recompute_height()
        self.update()

    def _recompute_height(self):
        if not self.lines:
            self.setFixedHeight(0)
            return
        s = self.scale
        line_h = self.LINE_HEIGHT_BASE * s
        pad_v = self.PAD_V_BASE * s
        # 이벤트가 1개면 1줄 높이, 2개면 2줄, 3개면 3줄 높이에 딱 맞춰 프레임 크기가 정해짐
        total_h = pad_v * 2 + line_h * len(self.lines)
        self.setFixedHeight(int(round(total_h)))

    def paintEvent(self, event):
        if not self.lines:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        s = self.scale
        w = self.width()
        h = self.height()
        radius = 8 * s   # CandleBox와 동일한 라운드 값 -> 시계 박스와 같은 모양의 프레임

        # ---- 프레임 배경: 시계 박스와 동일한 배경색/라운드 처리 ----
        path = QPainterPath()
        path.addRoundedRect(0, 0, w, h, radius, radius)
        painter.fillPath(path, BG_COLOR)

        pad_v = self.PAD_V_BASE * s
        pad_h = self.PAD_H_BASE * s
        line_h = self.LINE_HEIGHT_BASE * s

        font_size = max(9, round(12 * s))
        font = QFont("SF Pro Text", font_size)  # 메뉴 글자 크기 정도로 작게
        painter.setFont(font)
        painter.setPen(TEXT_COLOR)  # 흰색

        y = pad_v
        for code, kind, time_str, value_str in self.lines:
            if kind == "countdown":
                text = f"{code}   {time_str}  /  {value_str}"
            else:  # preview
                text = f"{code}   {value_str}"
            painter.drawText(
                int(round(pad_h)), int(round(y)), int(round(w - 2 * pad_h)), int(round(line_h)),
                Qt.AlignLeft | Qt.AlignVCenter, text
            )
            y += line_h
        painter.end()


# ----------------------------------------------------------------------
# Breaking News 매니저: Finnhub 뉴스 API(general + crypto 카테고리)를
# 10초마다 조회해서, 그 사이(폴링 창) 새로 올라온 기사 중 대표 헤드라인 딱 하나만 뽑아 보여줌
#   - 무료 플랜(분당 60건) 기준 10초 간격으로 카테고리별 조회 (NEWS_POLL_INTERVAL_SEC)
#   - 켜져 있을 때만 동작, 네트워크 호출은 백그라운드 스레드에서
#   - Finnhub 뉴스 엔드포인트는 서버 쪽 키워드 검색/중요도 점수를 제공하지 않아서, 카테고리
#     전체를 받아온 뒤 클라이언트에서 단어 경계 매칭으로 필터링 + 매칭 개수로 순위를 매김
#   - 대표 선정 규칙:
#     1) 이번 창의 새 기사 중 키워드에 걸리는 게 있으면, 매칭된 키워드 개수가 가장 많은 것
#        (동점이면 더 최근 발행된 것)
#     2) 키워드에 걸리는 새 기사가 하나도 없으면, 이번 창의 새 기사 중 가장 최근(첫 번째) 것
#     3) 이번 창에 새 기사 자체가 없으면(정말 아무 것도 안 올라옴), 지금 보여주던 걸 계속 반복
#   - 뉴스를 쌓아두지 않음: 매 폴링마다 대표 헤드라인 1개로 교체되는 방식이라, 오래된 뉴스가
#     계속 밀려서 쌓이는 문제가 생기지 않음
# ----------------------------------------------------------------------
class BreakingNewsManager(QObject):
    changed = Signal()

    def __init__(self, api_key, keywords, categories):
        super().__init__()
        self.api_key = api_key
        self.keywords = keywords
        self.categories = categories
        self.enabled = False
        self._lock = threading.Lock()
        self._last_poll = 0.0
        self._seen_ids = set()        # 중복 노출 방지 (Finnhub 기사 id 기준)
        self._seen_order = []         # _seen_ids 크기 제한용 (오래된 것부터 제거)
        self.items = []                # 최근 NEWS_ROTATION_WINDOW_SEC 이내 키워드 매칭된 뉴스 목록
                                        # [{"title": str, "added_at": monotonic_time}], 오래된 순
        self._pos = -1                 # items 안에서 지금 보여주고 있는 위치
        self.current = None            # 지금 화면에 보여줘야 할 헤드라인 (문자열) 또는 None
        self._warned_no_key = False
        # 단어 경계(\b)로 매칭해서 "War"가 "award" 안의 "war"처럼 단어 일부에 우연히 걸리는 걸 방지
        self._keyword_patterns = [re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE) for kw in keywords]

    def set_enabled(self, enabled):
        was_enabled = self.enabled
        self.enabled = enabled
        if enabled and not was_enabled:
            self._poll_now()
        self.changed.emit()

    def _poll_now(self):
        self._last_poll = time.monotonic()
        threading.Thread(target=self._fetch, daemon=True).start()

    def tick(self):
        """1초마다 ClockWindow.tick()에서 호출됨. 꺼져있으면 아무 일도 안 함(요청 없음)."""
        if not self.enabled:
            return
        if time.monotonic() - self._last_poll >= NEWS_POLL_INTERVAL_SEC:
            self._poll_now()

    def _keyword_match_count(self, title):
        # 진짜 '중요도' 점수는 이 API에 없어서, 우리 키워드 목록에 몇 개나 걸리는지를
        # 대신 근사치로 사용함 (여러 핵심 키워드가 동시에 걸릴수록 더 핵심적인 뉴스일 가능성이 높다고 가정)
        return sum(1 for p in self._keyword_patterns if p.search(title))

    def _prune_locked(self):
        """호출자가 이미 self._lock을 잡고 있는 상태에서 호출.
        NEWS_ROTATION_WINDOW_SEC보다 오래 전에 추가된 항목을 로테이션에서 제거함."""
        cutoff = time.monotonic() - NEWS_ROTATION_WINDOW_SEC
        self.items = [it for it in self.items if it["added_at"] >= cutoff]

    def advance(self):
        """마퀴가 한 바퀴 다 돌리면(BreakingNewsPanel._on_tick) 호출됨.
        최근 NEWS_ROTATION_WINDOW_SEC 이내 매칭된 뉴스를 순서대로 순환시켜 다음 문구를 반환.
        로테이션이 완전히 비어있으면(최근 창 안에 매칭된 뉴스가 없으면) 지금까지의 self.current를
        그대로 반환함(있으면 반복 노출, 처음부터 없으면 None)."""
        with self._lock:
            self._prune_locked()
            if self.items:
                self._pos = (self._pos + 1) % len(self.items)
                self.current = self.items[self._pos]["title"]
            return self.current

    def _fetch_category(self, category):
        """카테고리 하나를 조회. 실패하면 빈 리스트를 반환하고 콘솔에 원인을 남김.

        참고: Finnhub의 news id는 시간순으로 증가하지 않음(실제로 확인해보니 더 나중에 발행된
        기사가 더 작은 id를 받는 경우가 있었음). 그래서 minId로 '마지막으로 본 최대 id 이후만'
        받아오는 방식은 쓰지 않음 - 우연히 큰 id를 한 번 보면, 그 뒤에 올라온 진짜 새 기사가
        더 작은 id를 받아서 영영 필터에 걸려 빠지는 문제가 생길 수 있기 때문. 대신 매번 카테고리
        전체를 받아온 뒤, id 기준 client-side dedup(_seen_ids)만으로 새 기사를 판별함."""
        try:
            params = {"category": category, "token": self.api_key}
            url = f"{FINNHUB_BASE}/news?" + urllib.parse.urlencode(params)
            # User-Agent가 없으면 일부 서버(WAF/CDN)가 봇 요청으로 보고 403을 내려주는 경우가 있어
            # 브라우저와 비슷한 User-Agent를 명시적으로 붙여서 요청함
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) TradeTime/1.1",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
            if not isinstance(data, list):
                return []
            return data
        except urllib.error.HTTPError as e:
            # 403/401 등은 원인이 다양해서(키 오류/플랜 제한 등) 응답 본문을 같이 찍어서 원인 파악을 도움
            try:
                body = e.read().decode(errors="replace")
            except Exception:
                body = ""
            print(f"[Breaking News 오류] {category} 조회 실패: HTTP {e.code} {e.reason} - {body[:300]}")
            return []
        except Exception as e:
            print(f"[Breaking News 오류] {category} 조회 실패: {e}")
            return []

    def _fetch(self):
        if not self.api_key or self.api_key.startswith("YOUR_"):
            if not self._warned_no_key:
                self._warned_no_key = True
                print("[Breaking News] FINNHUB_API_KEY가 설정되지 않았습니다. "
                      "코드 상단의 FINNHUB_API_KEY를 finnhub.io에서 발급받은 키로 교체하세요.")
            return

        all_articles = []
        for category in self.categories:
            all_articles.extend(self._fetch_category(category))

        emit = False
        with self._lock:
            # 매 폴링(10초)마다 로테이션 창(NEWS_ROTATION_WINDOW_SEC)보다 오래된 항목을 먼저 정리.
            # 새 기사가 하나도 안 들어와도 이 정리는 항상 실행되어야 "N분 지나면 자동으로 빠짐"이 보장됨
            self._prune_locked()

            # 이번 폴링 창에서 처음 보는(아직 안 본 id) 기사만 추려냄 (키워드 매칭 여부와 무관하게)
            new_articles = []
            for art in all_articles:
                aid = art.get("id")
                headline = (art.get("headline") or "").strip()
                if not aid or not headline:
                    continue
                if aid in self._seen_ids:
                    continue
                self._seen_ids.add(aid)
                self._seen_order.append(aid)
                if len(self._seen_order) > NEWS_SEEN_HISTORY_MAX:
                    old = self._seen_order.pop(0)
                    self._seen_ids.discard(old)
                new_articles.append(art)

            was_empty = not self.items
            now = time.monotonic()
            for art in new_articles:
                headline = art["headline"].strip()
                if self._keyword_match_count(headline) > 0:
                    self.items.append({"title": headline, "added_at": now})

            if was_empty and self.items:
                # 로테이션이 비어있다가(화면이 놀고 있었을 수 있음) 방금 처음(또는 다시) 채워짐
                # -> 패널이 대기 중이었다면 바로 시작할 수 있게 시작점을 지정
                self._pos = 0
                self.current = self.items[0]["title"]
                emit = True
            elif not self.items and new_articles:
                # 로테이션엔 여전히 아무 것도 없음(최근 창 안에 키워드 매칭 뉴스 전무)
                # -> 새로 들어온 것 중 가장 최근 걸 임시로 보여줌(로테이션엔 쌓지 않음)
                newest = max(new_articles, key=lambda a: a.get("datetime") or 0)
                self.current = newest["headline"].strip()
                emit = True

        if emit:
            self.changed.emit()


# ----------------------------------------------------------------------
# Breaking News 표시줄: 시계 박스와 동일한 프레임 안에서 텍스트가
# 오른쪽 -> 왼쪽으로 천천히 흰색으로 흐름. manager가 10초마다 뽑아주는 대표 헤드라인
# 하나만 보여주고, 다음 폴링까지 새로 안 바뀌면 같은 문구를 계속 반복함
# ----------------------------------------------------------------------
class BreakingNewsPanel(QWidget):
    LINE_HEIGHT_BASE = MarketEventsPanel.LINE_HEIGHT_BASE
    PAD_V_BASE = MarketEventsPanel.PAD_V_BASE
    PAD_H_BASE = MarketEventsPanel.PAD_H_BASE

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.scale = 1.0
        self.text = ""
        self._x = 0.0
        self.setFixedHeight(0)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(NEWS_SCROLL_TICK_MS)

    def set_scale(self, scale):
        """MarketEventsPanel과 같은 scale을 공유해서 프레임 라운드/여백 비율을 맞춤."""
        self.scale = scale
        self._recompute_height()
        self.update()

    def has_content(self):
        return bool(self.text)

    def clear(self):
        self.text = ""
        self._recompute_height()
        self.update()

    def _recompute_height(self):
        if not self.text:
            self.setFixedHeight(0)
            return
        s = self.scale
        line_h = self.LINE_HEIGHT_BASE * s
        pad_v = self.PAD_V_BASE * s
        self.setFixedHeight(int(round(pad_v * 2 + line_h)))

    def _current_font(self):
        font_size = max(9, round(12 * self.scale))
        return QFont("SF Pro Text", font_size)

    def _load_from(self, title):
        self.text = title or ""
        self._x = float(self.width())   # 오른쪽 바깥에서부터 시작

    def notify_manager_changed(self):
        """manager.changed 신호로 호출됨. 지금 화면이 비어있는 상태(막 켜졌거나, 로테이션이
        방금 처음 채워짐)일 때만 즉시 시작함. 이미 뭔가 흐르고 있으면 끼어들지 않고, 그 문구가
        한 바퀴 다 돌 때(_on_tick) manager.advance()로 자연스럽게 다음으로 넘어가게 둠."""
        if not self.text and self.manager.current:
            self._load_from(self.manager.current)
        self._recompute_height()
        self.update()

    def _on_tick(self):
        if not self.manager.enabled or not self.text:
            return

        self._x -= NEWS_SCROLL_SPEED_PX * self.scale

        fm = QFontMetrics(self._current_font())
        text_w = fm.horizontalAdvance(self.text)

        if self._x < -(text_w + NEWS_SCROLL_GAP_PX * self.scale):
            # 한 바퀴 다 돌았음 -> 로테이션(최근 10분 이내 매칭 뉴스)에서 다음 걸로 순환.
            # 로테이션이 비어있으면 advance()가 지금 문구를 그대로 돌려줘서 반복 노출됨
            title = self.manager.advance()
            self._load_from(title)

        self.update()

    def paintEvent(self, event):
        if not self.text:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        s = self.scale
        w = self.width()
        h = self.height()
        radius = 8 * s   # CandleBox / MarketEventsPanel과 동일한 라운드 값

        path = QPainterPath()
        path.addRoundedRect(0, 0, w, h, radius, radius)
        painter.fillPath(path, BG_COLOR)

        font = self._current_font()
        painter.setFont(font)
        painter.setPen(TEXT_COLOR)

        # 텍스트가 프레임 밖으로 삐져나가 보이지 않도록 프레임 모양대로 클리핑
        painter.save()
        painter.setClipPath(path)
        fm = QFontMetrics(font)
        text_h = fm.height()
        text_w = fm.horizontalAdvance(self.text)
        y = int(round((h - text_h) / 2.0))
        painter.drawText(int(round(self._x)), y, text_w + 8, text_h, Qt.AlignLeft | Qt.AlignVCenter, self.text)
        painter.restore()
        painter.end()


class ClockWindow(QWidget):
    def __init__(self):
        super().__init__()
        cfg = load_config()
        self.scale = cfg["scale"]
        self.opacity = cfg["opacity"]
        self._drag_pos = None

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowOpacity(self.opacity)

        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(OUTER_MARGIN, OUTER_MARGIN, OUTER_MARGIN, OUTER_MARGIN)
        self._main_layout.setSpacing(4)  # 박스 <-> 이벤트 프레임 간격을 타이트하게 고정 (기본값에 의존하지 않음)
        self._layout_spacing = 4

        self.box = CandleBox("4시간봉")
        self._main_layout.addWidget(self.box)

        # Breaking News / Market Events 프레임은 실제로 보여줄 내용이 있을 때만
        # _sync_events_layout()이 레이아웃에 넣고 빼서, 꺼져있을 때 유령 간격이 생기지 않게 함
        # (우선순위: Breaking News가 위, Market Events가 아래)
        self.events_panel = MarketEventsPanel()
        self.breaking_news_manager = BreakingNewsManager(FINNHUB_API_KEY, BREAKING_NEWS_KEYWORDS, NEWS_CATEGORIES)
        self.breaking_news_panel = BreakingNewsPanel(self.breaking_news_manager)
        self.breaking_news_manager.changed.connect(self._on_news_changed)

        self.triggered_hour_key = None
        self.alert_sound = cfg["alert_sound"]  # 트레이 메뉴에서 바꿀 수 있는 현재 선택된 효과음
        self.blink_end_time = None             # 테두리 점멸을 멈춰야 할 monotonic 시각

        # 알람 목록: [{"time": "07:30", "repeat": "daily"|"once"}, ...]
        # (이전 버전엔 문자열 리스트였을 수 있어 호환 처리)
        self.custom_alarms = []
        for a in cfg.get("custom_alarms", []):
            if isinstance(a, str):
                self.custom_alarms.append({"time": a, "repeat": "daily"})
            elif isinstance(a, dict) and "time" in a:
                self.custom_alarms.append({"time": a["time"], "repeat": a.get("repeat", "daily")})
        self.custom_alarm_last_trigger = {}     # {"07:30": date(...)} - 하루에 한 번만 울리도록

        # 시계 숫자에 표시할 시간대 (도시 선택). 세그먼트 바는 항상 UTC 기준이라 이 값과 무관함
        self.display_tz_name = cfg.get("timezone", DEFAULT_TZ_NAME)
        try:
            self.display_tz = ZoneInfo(self.display_tz_name)
        except Exception:
            self.display_tz_name = DEFAULT_TZ_NAME
            self.display_tz = KST

        # Market Events는 display_tz가 이미 만들어진 뒤에 초기화해야 함
        # (켜는 순간 changed 신호가 바로 발생해서 _refresh_events_panel이 display_tz를 참조하기 때문)
        self.market_events_enabled = cfg.get("market_events_enabled", False)
        self.events_manager = MarketEventsManager(FRED_API_KEY)
        self.events_manager.changed.connect(self._refresh_events_panel)
        self.events_manager.set_enabled(self.market_events_enabled)

        self.breaking_news_enabled = cfg.get("breaking_news_enabled", False)
        self.breaking_news_manager.set_enabled(self.breaking_news_enabled)

        self.font_weight = cfg.get("font_weight", DEFAULT_FONT_WEIGHT)
        self.box.font_weight = self.font_weight  # apply_scale()이 첫 레이아웃을 계산할 때 반영되도록 미리 설정

        self.alert_color_hex = cfg.get("alert_color", DEFAULT_ALERT_COLOR_HEX)
        self.box.alert_color = QColor(self.alert_color_hex) if self.alert_color_hex else None

        self.reminder_minutes = cfg.get("reminder_minutes", DEFAULT_REMINDER_MINUTES)

        self.apply_scale(self.scale)
        self._sync_events_layout()  # 시작 시점의 Breaking News / Market Events 표시 상태를 레이아웃에 반영

        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self.tick)
        self.tick_timer.start(1000)
        self.tick()

        self.blink_timer = QTimer(self)
        self.blink_timer.timeout.connect(self.blink)
        self.blink_timer.start(BLINK_INTERVAL_MS)

    # ---------------- 시간 계산 ----------------
    def tick(self):
        now = datetime.now(timezone.utc)
        now_display = now.astimezone(self.display_tz)

        # 4시간봉 (UTC 0,4,8,12,16,20시 기준) - 세그먼트 바 진행률 계산용
        # 주의: 이 부분은 표시 시간대 선택과 무관하게 항상 UTC 기준 (글로벌 스탠다드) 그대로 둔다
        block_start_hour = (now.hour // 4) * 4
        block_start = now.replace(hour=block_start_hour, minute=0, second=0, microsecond=0)
        block_end = block_start + timedelta(hours=4)
        elapsed = now - block_start

        filled_segments = min(int(elapsed.total_seconds() // 3600), 3)
        partial = (elapsed.total_seconds() % 3600) / 3600.0
        self.box.update_time(now_display, filled_segments=filled_segments, partial_fraction=partial)

        # 리마인더: 정각이 되기 N분 전부터 시계 숫자를 알림색으로, 테두리도 10초 점멸
        # (N=reminder_minutes, 사용자가 메뉴에서 설정. 0=Off면 이 알림 자체를 건너뜀)
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        remaining_to_hour = next_hour - now
        hour_key = next_hour.isoformat()

        if self.reminder_minutes > 0:
            reminder_window = timedelta(minutes=self.reminder_minutes)
            if remaining_to_hour <= reminder_window:
                if self.triggered_hour_key != hour_key:
                    self.triggered_hour_key = hour_key
                    self.box.set_text_pink(True)  # 정각까지 알림색 유지되는 기본 상태
                    self.trigger_alarm(blink_text=True)  # 처음 10초는 테두리와 함께 점멸
                    send_mac_notification(
                        "Hourly Segment Closing Soon",
                        f"{self.reminder_minutes} minutes until the next 1-hour segment fills"
                    )
            else:
                # 정각이 지나 리마인더 구간이 다 지나면 시계 색을 다시 흰색으로, 그 순간 알림음 한 번
                if self.box.text_pink_active:
                    self.box.set_text_pink(False)
                    play_sound(self.alert_sound, volume=ALERT_VOLUME, repeat=1)
        else:
            # Off: 혹시 이전에 켜져 있던 상태였다면 깨끗하게 흰색으로 되돌림 (소리는 울리지 않음)
            if self.box.text_pink_active:
                self.box.set_text_pink(False)

        # 사용자가 추가한 알람 체크 (표시 중인 시간대 기준)
        hm = now_display.strftime("%H:%M")
        today = now_display.date()
        matched = [a for a in self.custom_alarms if a["time"] == hm]
        for alarm in matched:
            key = alarm["time"]
            if self.custom_alarm_last_trigger.get(key) == today:
                continue  # 이미 오늘 울렸음 (같은 초 안에서 tick 여러 번 도는 것 방지)
            self.custom_alarm_last_trigger[key] = today
            # 테두리+시계 숫자를 함께 알림색으로 10초 깜빡이고, 사운드 재생
            self.trigger_alarm(blink_text=True)
            send_mac_notification("TradeTime Alarm", f"Your {hm} alarm")
            if alarm["repeat"] == "once":
                self.remove_custom_alarm(hm)

        # Market Events (경제지표 카운트다운) - 꺼져있으면 manager.tick() 안에서 바로 리턴됨
        self.events_manager.tick()
        self._refresh_events_panel()

        # Breaking News (키워드 속보 티커) - 꺼져있으면 manager.tick() 안에서 바로 리턴됨
        self.breaking_news_manager.tick()

    def _refresh_events_panel(self):
        if not hasattr(self, "display_tz") or not hasattr(self, "events_panel"):
            return  # 초기화가 아직 다 안 끝난 시점에 신호가 오는 경우 방지
        lines = self.events_manager.get_display_lines(self.display_tz)
        self.events_panel.update_lines(lines)
        self._sync_events_layout()

    def _on_news_changed(self):
        if not hasattr(self, "breaking_news_panel"):
            return  # 초기화가 아직 다 안 끝난 시점에 신호가 오는 경우 방지
        self.breaking_news_panel.notify_manager_changed()
        self._sync_events_layout()

    def _sync_events_layout(self):
        """Breaking News(우선) / Market Events 프레임 중 실제로 보여줄 내용이 있는 것만
        순서대로(뉴스 먼저, 이벤트 다음) 레이아웃에 넣고, 없는 건 레이아웃에서 완전히 빼서
        타이트한 간격을 항상 보장한다 (0으로 높이만 줄이는 방식은 위/아래 조합에 따라
        유령 간격이 생길 수 있어 쓰지 않음)."""
        if not hasattr(self, "_main_layout"):
            return

        desired = []
        if self.breaking_news_panel.has_content():
            desired.append(self.breaking_news_panel)
        if self.events_panel.has_content():
            desired.append(self.events_panel)

        current = [self._main_layout.itemAt(i).widget() for i in range(1, self._main_layout.count())]
        if current != desired:
            for w in (self.breaking_news_panel, self.events_panel):
                self._main_layout.removeWidget(w)
                w.hide()
            for w in desired:
                self._main_layout.addWidget(w)
                w.show()

        self._resize_to_content()

    def _resize_to_content(self):
        """박스 + (있으면) Breaking News + (있으면) Market Events 표시줄 높이를 합쳐서 창 크기를 다시 맞춤."""
        if not hasattr(self, "_main_layout"):
            return
        w = int(BASE_WIDTH * self.scale) + OUTER_MARGIN * 2
        visible = [self._main_layout.itemAt(i).widget() for i in range(self._main_layout.count())]
        total_content_h = sum(v.height() for v in visible)
        gaps = self._layout_spacing * max(0, len(visible) - 1)
        h = total_content_h + gaps + OUTER_MARGIN * 2
        self.resize(w, h)
        self.update()
        self.events_panel.update()
        self.breaking_news_panel.update()

    def trigger_alarm(self, blink_text=False):
        """테두리를 (사용자가 고른) 알림색으로 10초간 점멸시키고, 효과음을 볼륨 높여 3번 연속 재생한다.
        blink_text=True면 시계 숫자도 테두리와 함께 깜빡였다가, 10초 후 테두리와 함께 흰색으로 돌아온다.
        알림색이 Off(None)이면 시각 효과는 안 뜨고 소리만 울린다."""
        self.box.start_border_blink(blink_text=blink_text)
        self.blink_end_time = time.monotonic() + BLINK_DURATION_SECONDS
        play_sound(self.alert_sound, volume=ALERT_VOLUME, repeat=SOUND_REPEAT)

    def blink(self):
        if self.box.border_alert_active:
            if self.blink_end_time is not None and time.monotonic() < self.blink_end_time:
                self.box.toggle_border_blink()
            else:
                self.box.stop_border_blink()
                self.blink_end_time = None

    # ---------------- 크기 / 투명도 / 시간대 / 알람 / 폰트굵기 ----------------
    def save_settings(self):
        save_config({
            "scale": self.scale,
            "opacity": self.opacity,
            "alert_sound": self.alert_sound,
            "timezone": self.display_tz_name,
            "custom_alarms": self.custom_alarms,
            "font_weight": self.font_weight,
            "alert_color": self.alert_color_hex,
            "reminder_minutes": self.reminder_minutes,
            "market_events_enabled": self.market_events_enabled,
            "breaking_news_enabled": self.breaking_news_enabled,
        })

    def set_market_events_enabled(self, enabled):
        self.market_events_enabled = enabled
        # set_enabled() 안에서 changed 신호가 동기적으로 발생 -> _refresh_events_panel()이
        # lines를 다시 계산해서(꺼져있으면 자동으로 []) _sync_events_layout()까지 처리함
        self.events_manager.set_enabled(enabled)
        self._sync_events_layout()
        self.save_settings()

    def set_breaking_news_enabled(self, enabled):
        self.breaking_news_enabled = enabled
        self.breaking_news_manager.set_enabled(enabled)
        if not enabled:
            self.breaking_news_panel.clear()
        self._sync_events_layout()
        self.save_settings()

    def apply_alert_color(self, hex_or_none):
        self.alert_color_hex = hex_or_none
        self.box.set_alert_color(QColor(hex_or_none) if hex_or_none else None)
        self.save_settings()

    def apply_reminder_minutes(self, minutes):
        self.reminder_minutes = minutes
        self.triggered_hour_key = None  # 다음 tick에서 새 기준으로 다시 판단하도록
        if minutes == 0 and self.box.text_pink_active:
            self.box.set_text_pink(False)
        self.save_settings()

    def apply_font_weight(self, weight):
        self.font_weight = weight
        self.box.set_font_weight(weight)
        # 굵기가 바뀌면 글자 크기/박스 높이도 달라질 수 있으므로 창 크기도 다시 맞춤
        self._resize_to_content()
        self.save_settings()

    def _find_alarm(self, time_str):
        for a in self.custom_alarms:
            if a["time"] == time_str:
                return a
        return None

    def add_custom_alarm(self, time_str, repeat="daily"):
        if self._find_alarm(time_str) is not None:
            return False
        if len(self.custom_alarms) >= MAX_CUSTOM_ALARMS:
            return False
        self.custom_alarms.append({"time": time_str, "repeat": repeat})
        self.custom_alarms.sort(key=lambda a: a["time"])
        self.save_settings()
        return True

    def remove_custom_alarm(self, time_str):
        alarm = self._find_alarm(time_str)
        if alarm is not None:
            self.custom_alarms.remove(alarm)
            self.custom_alarm_last_trigger.pop(time_str, None)
            self.save_settings()

    def edit_custom_alarm(self, old_time, new_time, repeat=None):
        alarm = self._find_alarm(old_time)
        if alarm is None:
            return False
        if new_time != old_time and self._find_alarm(new_time) is not None:
            return False
        alarm["time"] = new_time
        if repeat is not None:
            alarm["repeat"] = repeat
        self.custom_alarms.sort(key=lambda a: a["time"])
        self.custom_alarm_last_trigger.pop(old_time, None)
        self.save_settings()
        return True

    def set_timezone(self, tz_name):
        try:
            self.display_tz = ZoneInfo(tz_name)
            self.display_tz_name = tz_name
            self.save_settings()
            self.tick()  # 즉시 반영
        except Exception as e:
            print(f"[시간대 오류] {tz_name}: {e}")

    def apply_scale(self, scale):
        scale = max(MIN_SCALE, min(MAX_SCALE, scale))
        self.scale = scale
        self.box.set_scale(scale)  # 박스 내부에서 폰트/높이를 알아서 재계산함
        self.events_panel.set_scale(scale)  # Market Events 프레임도 같은 scale로 맞춤
        self.breaking_news_panel.set_scale(scale)  # Breaking News 프레임도 같은 scale로 맞춤
        self._resize_to_content()
        self.save_settings()

    def apply_opacity(self, opacity):
        opacity = max(MIN_OPACITY, min(MAX_OPACITY, opacity))
        self.opacity = opacity
        self.setWindowOpacity(opacity)
        self.save_settings()

    # ---------------- 드래그 이동 ----------------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None


# ----------------------------------------------------------------------
# 트레이(메뉴바) 아이콘 - 크기/투명도/효과음 조절 메뉴
# ----------------------------------------------------------------------
# 메뉴바(트레이) 아이콘 원본 PNG를 base64로 내장 (사용자가 디자인한 로고, 별도 파일 없이 동작하도록)
# ----------------------------------------------------------------------
# 앱 정보 (About / Donation 팝업에 사용)
# ----------------------------------------------------------------------
APP_VERSION = "1.1 (26)"
APP_AUTHOR = "SR Shin"
APP_EMAIL = "gotossr@gmail.com"
APP_COPYRIGHT_YEAR = "2026"

# 후원 지갑 주소
# ⚠️ BTC 주소 중간에 있던 문자가 원본 자료에서 "×"(곱셈기호)로 보였는데, bech32 BTC 주소에는
#   쓰이지 않는 문자라 일반 "x"로 바로잡아 넣었습니다. 실제 지갑 주소와 다르면 꼭 알려주세요!
DONATION_USDT_ADDRESS = "THiTwBT9PnVGaf2WDRZXMtekwjbyBEEmgh"
DONATION_BTC_ADDRESS = "bc1qqkdw2w4n8x3k055974k5jp5ej8wufkh8yfuv7x"

DONATION_QR_PNG_BASE64 = (
    "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"
    "89Muya6umpiak7XutbW7DXevqEPrFHU9e1L3XNwbsLe53qF+WwOzoWwf2KfY9+zHtB+v7Q/b33aAfaD+J8ufNh5kHCxtRBrnNPY1"
    "ZTV1NSc3dx4KPdTW4tVy8GfHn3e1mrZWHdY9vOII9UjxkYGjhUf7j0mP9R7PPP6wbXrb7RNJJ66cjD7ZcSrs1Jlfgn45cZpz+ugZ"
    "7zOtZz3PHjrHPtd03u18Y7tr+8FfXX892OHW0XjB/ULzRY+LLZ2TOo9c8r10/HLA5V+u8K6cvxpxtfNa/LUb11Ovd90Q3nh6M/fm"
    "y1sFtz7cXnSHcKf0rubd8nuG96p/s/2tocut6/D9gPvtD2If3H4oePj8kfzRx+7ix7TH5U9MntQ+dX7a2hPUc/HZlGfdz6XPP/SW"
    "/K71+8YXNi9++sPvj/a+pL7ul7KXA38ue6X/atdfLn+19Uf133ud9/rDm9K3+m9r3rHfnX6f+P7Jh1kfSR8rPtl+avkc9vnOQN7A"
    "gJQv4w/+CmBAubXJAODPXQDQkgFgwH0jdYpqfzhoiGpPO4jAf8KqPeSgwT+XevhPH90L/26uA7BvBwBWUJ+eCkAUDYA4D4BOnDjS"
    "hvdyg/tOpRHh3mAr71N6Xjr4N6bak34V9+gzUKq6gNHnfwG9cIMARX5VEQAA5PxJREFUeNrs/Xe8ZUWVPg4/VTudcFPnSNOkBpom"
    "NznnIKCCoBhHREyYZhADMo4BEyMIJgygDsOooKQZQTIoOTShgSZ30znfdNJOtX5/VO29a4dz77ndTev3fT1+rnTfPmef2rWraq31"
    "rGc9iy1ZvJjwz9f/my/656P7x3kxAFvweTDW2duI/rkK/vn65+v/1Ze5qfuX/mkB/gEMMOGteAJpW9KJZSl+DwPDpo9wpO/t3Nol"
    "78x+ZrRrqH8n7a36fwvuVb/eWEeenSvOGFgHVthkHZrqopf45/79u76EIJB67ExbWG3WWJtlJP/cfp2yNksyuxQpf8Iz5BY1K3pL"
    "2zFT4XfJ92Y+QTTaTReMk0Z8F9Ovm5kYxljqAGXtrtfuYeR+rx0WYOBmZxuY/3Mb/D8edgHgYKnFwLUHLxcBy8RU+UXMUpdkyeUY"
    "JdfIrMTk91Qcw2U8NM6Y9j1MjrtgzMXbOxld8l5Kxs94BwcWdTypLHp/emKSQ1LdG2dMjUfOU26uGNqZ7BHjXtbZafTPDfz/Czs4"
    "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"
)

DOG_PHOTO_JPEG_BASE64 = (
    "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"
)

CAT_PHOTO_JPEG_BASE64 = (
    "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"
)



TRAY_ICON_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACwAAAAsCAYAAAAehFoBAAAEvUlEQVR4nO2YX0hbVxjAv/PnRl0WtollrKx90W7zZTLow/awuj4J"
    "Utgo9LEv1cJe9tCHUnwoFaRVN1B822zJrUmKECuMlYkpmyu4OjOlUrF02gqLoKVsaIhGTe4539mDO9fbaxKTGmdh+b3l5uR8v/Pl"
    "O/d+5wKUKPE/hVIKnPMXrtXW1pb19vae7enpOQ0AwBgreoyCYYy9IGIYBpw6dertwcHBi4lEYkkppYLB4JcA8NLB3DEKhhACnHMg"
    "hNjXjh49yi9duvTxo0ePflD/IqW0hBAp0zSbChXOFOPIkSOspaXlk2g06s9rEkrpjpWeOHHiDdM0m+Lx+IJbNJ1OJ5VS6ubNm835"
    "CmeKUV9f/6Zpmk0rKyt/KqWUZVkbOVfKGANKqX2tsrKSNDc3v3///v1vlQPLsjaklJbzcz7C2WKcP3/+g7GxsV53jHg8vpBxpe4A"
    "dXV1FV1dXZ8vLS1N6QkQUQohUogolYvdhLPF6O7u/iJXjEQisZQ1wxUVFXDmzJl3h4eHrwohUnoSIUTKmc1MZBN2byJnDOec2WJk"
    "FD527Jhx+fLlT+fm5u66JTJlsxBhTXV1tXHlypX6QmNkFI7FYr/pAXoT5SuaTdgwDCCEwMmTJ9/q7+//am1t7bkeK4RI5RvDWcN2"
    "CgzDKBdCbBJCKGPMk7VmCgARgRACoVDo58OHD38EACCE2KSU8t1iKKUQEQVjzOPz+d7ZIYyIknNerpTCbJNIKdOEEEopLeiJkEwm"
    "/5ZSpgEAOOflucYiolBKIWPMoxd17969nh3ChBCaaQK90r1kXmcUEUW2Me5kLC8vz9++fbvV7/d/H41G13YIu0FEgYiCc16uRcfH"
    "x29wzsuOHz9+FhFFoZnOhY4xNTXV39fX93U4HJ5+9uwZAmzdt5VSL/5gcXHxgd44zttZMpn8KxwOX2hsbDwEANDe3t7g3GC5Np1+"
    "ODx9+nREb2b3b6SUFiLKSCRyraGhocp5+3M/XAAcGRZCpAC2a2x+fv6XW7dutYVCoV+fPHli6XFer9dXrKwCbJUcpZR3dnZ+MzIy"
    "ssIYA845SClBSrljvC1cVVVVAwAQiUSumab53Z07dxbW19ftlXLOIZ1OAyLunKUI+Hy+MsYYEEJAiKylvi3c0dFxenh4+MHExETS"
    "/pJzQESQUmauoyKCiEpKuWtbaQu3tbWNAmwVOKUUEDHnSg8KW5gxBkopO6OvKrbwqyzpJOPD4lWmJLzflIT3m6IKI6IQQmwWc043"
    "e+62nO2nx+N5vRhSuXhpYaUUCiE2ne3n48ePfwwGg1dDodDvhBD7xFFMChZGRJRSpg3DeA0AwLKs9Ugk0un3+28MDQ0tpVIpANju"
    "YQ9c2Ov1+hhjnlgsNjYwMNDe19f308zMjF23ujXcL/IW1p3a5OTk5PT09IeBQGBmdXVVAWy9INEloBumfDOrz3BFF9ZZu379+h/6"
    "mrNhKgTnRtWlRfJcYcElQSkFSmnWE0EuMp0THz58OBAIBDpHR0ef63/pQNAJo5TC3NzcXed5bmNjY2VwcPBiY2Pjob2+9C4aWpgQ"
    "Yh9wZ2dnI62trZ/V1NQYzrHu98IHghYwDAPC4fCFc+fOvef1eu3v9QvCAxctUeI/5h/szGJ/fsmfvwAAAABJRU5ErkJggg=="
)





# 후원 팝업용 강아지/고양이/QR 이미지 (base64 내장, 별도 파일 없이 동작)








def make_tray_icon():
    """사용자가 디자인한 로고(logo_s.png)를 메뉴바 아이콘으로 사용.
    혹시 이미지 디코딩에 실패하면(손상 등) 예전 방식의 기본 아이콘으로 대체된다."""
    try:
        png_bytes = base64.b64decode(TRAY_ICON_PNG_BASE64)
        pix = QPixmap()
        if pix.loadFromData(png_bytes, "PNG") and not pix.isNull():
            return QIcon(pix)
    except Exception as e:
        print(f"[트레이 아이콘 오류] {e}")

    # 폴백: 기본 아이콘
    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(247, 147, 26))
    p.setPen(Qt.NoPen)
    p.drawEllipse(2, 2, 28, 28)
    p.setPen(QColor(255, 255, 255))
    f = QFont("Helvetica", 14)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pix.rect(), Qt.AlignCenter, "B")
    p.end()
    return QIcon(pix)


def _pixmap_from_base64(b64_str, fmt=None):
    """base64 문자열을 QPixmap으로 디코딩. 실패하면 None 반환 (안전하게 다룸)."""
    try:
        raw = base64.b64decode(b64_str)
        pix = QPixmap()
        ok = pix.loadFromData(raw, fmt) if fmt else pix.loadFromData(raw)
        if ok and not pix.isNull():
            return pix
    except Exception as e:
        print(f"[이미지 디코딩 오류] {e}")
    return None


# ----------------------------------------------------------------------
# About TradeTime 서브메뉴 (마우스를 올리면 바로 펼쳐지는 정보 패널)
# ----------------------------------------------------------------------
def build_about_menu(parent_menu):
    about_menu = parent_menu.addMenu("About TradeTime")

    info_widget = QWidget()
    info_widget.setStyleSheet("background-color: #2b2b2b;")
    v = QVBoxLayout(info_widget)
    v.setContentsMargins(18, 14, 22, 14)
    v.setSpacing(4)

    title = QLabel("TradeTime")
    title.setStyleSheet("color: white; font-size: 15px; font-weight: 700;")
    v.addWidget(title)

    version_lbl = QLabel(f"Version {APP_VERSION}")
    version_lbl.setStyleSheet("color: #dddddd; font-size: 12px;")
    v.addWidget(version_lbl)

    author_lbl = QLabel(f"Created by: {APP_AUTHOR}")
    author_lbl.setStyleSheet("color: #dddddd; font-size: 12px;")
    v.addWidget(author_lbl)

    v.addSpacing(8)

    email_lbl = QLabel(APP_EMAIL)
    email_lbl.setStyleSheet("color: #dddddd; font-size: 12px;")
    v.addWidget(email_lbl)

    v.addSpacing(8)

    copyright_lbl = QLabel(f"Copyright (C) {APP_COPYRIGHT_YEAR} {APP_AUTHOR},\nall rights reserved")
    copyright_lbl.setStyleSheet("color: #999999; font-size: 11px;")
    v.addWidget(copyright_lbl)

    info_widget.setMinimumWidth(230)

    widget_action = QWidgetAction(about_menu)
    widget_action.setDefaultWidget(info_widget)
    about_menu.addAction(widget_action)
    return about_menu


# ----------------------------------------------------------------------
# 후원(Donation) 팝업 창
# ----------------------------------------------------------------------
def _make_copyable_address_row(label_text, address, parent_dialog):
    """주소를 박스 없이 일반 텍스트로 보여주되, 클립보드 복사 기능은 그대로 유지."""
    row = QWidget()
    v = QVBoxLayout(row)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(2)

    lbl = QLabel(label_text)
    lbl.setStyleSheet("color: white; font-size: 12px; font-weight: 700;")
    v.addWidget(lbl)

    addr_row = QHBoxLayout()
    addr_row.setSpacing(6)

    addr_lbl = QLabel(address)
    addr_lbl.setStyleSheet("color: #dddddd; font-family: Menlo; font-size: 11px;")
    addr_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
    addr_row.addWidget(addr_lbl)

    copy_btn = QPushButton("⧉")
    copy_btn.setFixedWidth(29)   # 기존 22px 대비 130%
    copy_btn.setToolTip("Copy to clipboard")
    copy_btn.setStyleSheet(
        "QPushButton { background-color: transparent; color: #999999; border: none; padding: 0px; font-size: 17px; }"
        "QPushButton:hover { color: white; }"
    )

    def do_copy():
        QGuiApplication.clipboard().setText(address)
        copy_btn.setText("✓")
        QTimer.singleShot(1200, lambda: copy_btn.setText("⧉"))

    copy_btn.clicked.connect(do_copy)
    addr_row.addWidget(copy_btn)
    addr_row.addStretch()

    v.addLayout(addr_row)
    return row


def show_donation_dialog(parent_window):
    dlg = QDialog(parent_window)
    dlg.setWindowTitle("Buy Me a Snack for Dog & Cat")
    dlg.setStyleSheet("background-color: #2b2b2b;")
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowStaysOnTopHint)
    bring_app_to_front()

    root = QVBoxLayout(dlg)
    root.setContentsMargins(22, 20, 22, 18)
    root.setSpacing(10)
    # 창이 사용자가 드래그(또는 초록색 확대 버튼)로 넓어지면서 레이아웃이 무너지는 걸 막기 위해
    # 다이얼로그 크기를 콘텐츠에 딱 맞게 고정(리사이즈 불가)함
    root.setSizeConstraint(QVBoxLayout.SetFixedSize)

    def divider():
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("background-color: #444; max-height: 1px; border: none;")
        return line

    # ---- 상단: 한글 소개(왼쪽) + 카카오페이 QR(오른쪽) 2단 배치 ----
    top_row = QHBoxLayout()
    top_row.setSpacing(18)

    # after 시안과 동일한 줄바꿈이 되도록 자동 word-wrap 대신 줄바꿈 위치를 직접 지정
    intro_kr = QLabel(
        "이 프로그램은 무료입니다.\n"
        "만약 이 프로그램이 당신의 트레이딩에\n"
        "도움이 되었다면, 저와 함께 살고 있는\n"
        "사랑스러운 강아지 구름이 🐶 와\n"
        "고양이 짠순이 🐱 의 간식값을 후원해\n"
        "주세요.\n"
        "여러분의 후원은 더 좋은 프로그램을\n"
        "만드는 데 큰 힘이 됩니다.\n"
        "감사합니다! ❤️❤️❤️"
    )
    intro_kr.setStyleSheet("color: white; font-size: 12px;")

    # 한글 소개 + 해외송금 안내문을 한 컬럼으로 묶어서 타이트하게 붙임
    kr_col = QWidget()
    kr_v = QVBoxLayout(kr_col)
    kr_v.setContentsMargins(0, 0, 0, 0)
    kr_v.setSpacing(8)
    kr_v.addWidget(intro_kr)

    overseas_note = QLabel("* 하단의 USDT, BTC는 해외 거래소 계좌\n및 지갑에서만 보내실 수 있습니다.")
    overseas_note.setStyleSheet("color: #cccccc; font-size: 12px;")
    kr_v.addWidget(overseas_note)

    # 텍스트 시작점을 QR 이미지 상단과 맞추기 위해 Qt.AlignTop으로 명시
    top_row.addWidget(kr_col, 0, Qt.AlignTop)
    # QR을 텍스트 그룹 바로 옆에 여백 없이 타이트하게 붙임(뒤에 늘어나는 stretch 없음)

    kakao_col = QWidget()
    kakao_v = QVBoxLayout(kakao_col)
    kakao_v.setContentsMargins(0, 0, 0, 0)
    kakao_v.setSpacing(6)

    qr_pix = _pixmap_from_base64(DONATION_QR_PNG_BASE64, "PNG")
    qr_lbl = QLabel()
    if qr_pix is not None:
        # 이전 132x132 대비 120% 확대
        qr_lbl.setPixmap(qr_pix.scaled(158, 158, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    kakao_v.addWidget(qr_lbl)

    kakao_lbl = QLabel("카카오페이\n(only users in Korea)")
    kakao_lbl.setStyleSheet("color: white; font-size: 12px; font-weight: 700;")
    kakao_v.addWidget(kakao_lbl)

    top_row.addWidget(kakao_col, 0, Qt.AlignTop)
    root.addLayout(top_row)

    root.addWidget(divider())

    # ---- 반려동물 사진 ----
    photos_row = QHBoxLayout()
    photos_row.setSpacing(18)

    def pet_column(name_text, photo_b64):
        col = QWidget()
        cv = QVBoxLayout(col)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(4)
        name_lbl = QLabel(f"{name_text} ❤️")
        name_lbl.setStyleSheet("color: white; font-size: 12px; font-weight: 700;")
        cv.addWidget(name_lbl)
        photo_lbl = QLabel()
        pix = _pixmap_from_base64(photo_b64, "JPEG")
        if pix is not None:
            photo_lbl.setPixmap(pix.scaled(190, 190, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        cv.addWidget(photo_lbl)
        cv.addStretch()
        return col

    photos_row.addWidget(pet_column("Goorumi", DOG_PHOTO_JPEG_BASE64))
    photos_row.addWidget(pet_column("Zansooni", CAT_PHOTO_JPEG_BASE64))
    photos_row.addStretch()
    root.addLayout(photos_row)

    # ---- 영문 소개 (after 시안과 동일한 줄바꿈으로 직접 지정) ----
    intro_en = QLabel(
        "This app is free.\n"
        "If it helps your trading, please consider buying a snack\n"
        "for my lovely dog Goorumi 🐶 and cat Zansooni 🐱 .\n"
        "Your support helps me continue building better trading tools.\n"
        "Thank you! ❤️❤️❤️"
    )
    intro_en.setStyleSheet("color: white; font-size: 12px;")
    root.addWidget(intro_en)

    # ---- USDT / BTC 주소 (박스 없는 형태, 복사 기능은 그대로 유지) ----
    root.addWidget(_make_copyable_address_row("USDT (Tron / TRC20)", DONATION_USDT_ADDRESS, dlg))
    root.addWidget(_make_copyable_address_row("BTC (Native SegWit)", DONATION_BTC_ADDRESS, dlg))

    root.addWidget(divider())

    footer = QLabel(
        "* If you share your feedback on this program via the email below,\n"
        "we will take it into consideration for future updates. Also,\n"
        "if you have any suggestions for other trading apps you need,\n"
        "let us know, and we will prioritize them when developing new programs."
    )
    footer.setStyleSheet("color: #999999; font-size: 11px;")
    footer.setTextInteractionFlags(Qt.TextSelectableByMouse)
    root.addWidget(footer)

    # 이메일 주소: 마우스로 드래그해서 선택/복사 가능 + 원클릭 카피 버튼도 같이 제공
    email_row = QHBoxLayout()
    email_row.setSpacing(6)

    email_lbl = QLabel(f"> {APP_EMAIL}")
    email_lbl.setStyleSheet("color: #999999; font-size: 11px;")
    email_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
    email_row.addWidget(email_lbl)

    email_copy_btn = QPushButton("⧉")
    email_copy_btn.setFixedWidth(20)
    email_copy_btn.setToolTip("Copy to clipboard")
    email_copy_btn.setStyleSheet(
        "QPushButton { background-color: transparent; color: #777777; border: none; padding: 0px; font-size: 12px; }"
        "QPushButton:hover { color: white; }"
    )

    def do_copy_email():
        QGuiApplication.clipboard().setText(APP_EMAIL)
        email_copy_btn.setText("✓")
        QTimer.singleShot(1200, lambda: email_copy_btn.setText("⧉"))

    email_copy_btn.clicked.connect(do_copy_email)
    email_row.addWidget(email_copy_btn)
    email_row.addStretch()
    root.addLayout(email_row)

    # 폭/높이는 위 root.setSizeConstraint(SetFixedSize)가 콘텐츠에 맞춰 자동으로 고정함
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg


def build_tray(app, window: ClockWindow):
    tray = QSystemTrayIcon(make_tray_icon())
    tray.setToolTip("TradeTime")

    menu = QMenu()
    menu.setStyleSheet("QMenu { min-width: 240px; }")

    build_about_menu(menu)

    # ---- 팝업창(입력/알림)이 다른 앱 뒤에 가려지지 않고 항상 맨 앞에 뜨도록 하는 헬퍼들 ----
    def _prep_dialog(dlg):
        dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowStaysOnTopHint)
        bring_app_to_front()
        dlg.raise_()
        dlg.activateWindow()

    def get_text_input(title, label, default_text=""):
        dlg = QInputDialog(window)
        dlg.setWindowTitle(title)
        dlg.setLabelText(label)
        dlg.setTextValue(default_text)
        _prep_dialog(dlg)
        ok = dlg.exec() == QInputDialog.Accepted
        return dlg.textValue(), ok

    def get_choice_input(title, label, items, current_index=0):
        dlg = QInputDialog(window)
        dlg.setWindowTitle(title)
        dlg.setLabelText(label)
        dlg.setComboBoxItems(items)
        dlg.setComboBoxEditable(False)
        if 0 <= current_index < len(items):
            dlg.setTextValue(items[current_index])
        _prep_dialog(dlg)
        ok = dlg.exec() == QInputDialog.Accepted
        return dlg.textValue(), ok

    def show_warning(text):
        box = QMessageBox(QMessageBox.Warning, "TradeTime", text, QMessageBox.Ok, window)
        _prep_dialog(box)
        box.exec()

    def show_info(text):
        box = QMessageBox(QMessageBox.Information, "TradeTime", text, QMessageBox.Ok, window)
        _prep_dialog(box)
        box.exec()

    inc_size = QAction("Size +", menu)
    dec_size = QAction("Size -", menu)
    inc_size.triggered.connect(lambda: window.apply_scale(window.scale + SCALE_STEP))
    dec_size.triggered.connect(lambda: window.apply_scale(window.scale - SCALE_STEP))
    menu.addAction(inc_size)
    menu.addAction(dec_size)

    menu.addSeparator()

    inc_opacity = QAction("Opacity +", menu)
    dec_opacity = QAction("Opacity -", menu)
    inc_opacity.triggered.connect(lambda: window.apply_opacity(window.opacity + OPACITY_STEP))
    dec_opacity.triggered.connect(lambda: window.apply_opacity(window.opacity - OPACITY_STEP))
    menu.addAction(inc_opacity)
    menu.addAction(dec_opacity)

    menu.addSeparator()

    # ---- 리마인더: 정각 몇 분 전에 알릴지 ----
    reminder_menu = menu.addMenu("Reminder")

    def prompt_custom_reminder():
        text, ok = get_text_input(
            "Custom Reminder",
            "Minutes before the hour to alert (1-59):",
            default_text=str(window.reminder_minutes)
        )
        if not ok:
            return
        text = text.strip()
        if not text.isdigit() or not (1 <= int(text) <= 59):
            show_warning("Please enter a whole number between 1 and 59.")
            return
        window.apply_reminder_minutes(int(text))

    def rebuild_reminder_menu():
        reminder_menu.clear()
        group = QActionGroup(reminder_menu)
        group.setExclusive(True)
        preset_minutes = [m for _, m in REMINDER_PRESETS]
        is_custom = window.reminder_minutes not in preset_minutes

        for name, minutes in REMINDER_PRESETS:
            action = QAction(name, reminder_menu, checkable=True)
            if not is_custom and minutes == window.reminder_minutes:
                action.setChecked(True)
            action.triggered.connect(lambda checked=False, mm=minutes: window.apply_reminder_minutes(mm))
            group.addAction(action)
            reminder_menu.addAction(action)

        custom_label = f"Custom ({window.reminder_minutes} min)" if is_custom else "Custom..."
        custom_action = QAction(custom_label, reminder_menu, checkable=True)
        if is_custom:
            custom_action.setChecked(True)
        custom_action.triggered.connect(prompt_custom_reminder)
        group.addAction(custom_action)
        reminder_menu.addAction(custom_action)

    rebuild_reminder_menu()
    reminder_menu.aboutToShow.connect(rebuild_reminder_menu)

    menu.addSeparator()

    # ---- Market Events (경제지표 카운트다운 / 브레이킹 뉴스) ----
    # 두 항목은 서로 독립적으로 켜고 끌 수 있음 (동시에 켜면 Breaking News가 위, Market Events가 아래에 표시됨)
    events_menu = menu.addMenu("Market Events")

    econ_action = QAction("Economic Events", events_menu, checkable=True)
    econ_action.setChecked(window.market_events_enabled)
    econ_action.triggered.connect(lambda checked: window.set_market_events_enabled(checked))
    events_menu.addAction(econ_action)

    news_action = QAction("Breaking News", events_menu, checkable=True)
    news_action.setChecked(window.breaking_news_enabled)
    news_action.triggered.connect(lambda checked: window.set_breaking_news_enabled(checked))
    events_menu.addAction(news_action)

    menu.addSeparator()

    sound_menu = menu.addMenu("Alert Sound")
    sound_group = QActionGroup(menu)
    sound_group.setExclusive(True)

    def make_sound_selector(path):
        def _select():
            window.alert_sound = path
            window.save_settings()
            play_sound(path)  # 고르는 즉시 미리듣기 (Off는 play_sound 안에서 자동으로 무시됨)
        return _select

    for name, path in SOUND_CHOICES:
        action = QAction(name, sound_menu, checkable=True)
        if path == window.alert_sound:
            action.setChecked(True)
        action.triggered.connect(make_sound_selector(path))
        sound_group.addAction(action)
        sound_menu.addAction(action)

    menu.addSeparator()

    # ---- 알림(테두리 점멸 + 정각 텍스트) 색상 선택 ----
    color_menu = menu.addMenu("Alert Color")
    color_group = QActionGroup(menu)
    color_group.setExclusive(True)

    def make_color_selector(hex_or_none):
        def _select():
            window.apply_alert_color(hex_or_none)
        return _select

    for name, hex_val in ALERT_COLOR_CHOICES:
        action = QAction(name, color_menu, checkable=True)
        if hex_val == window.alert_color_hex:
            action.setChecked(True)
        action.triggered.connect(make_color_selector(hex_val))
        color_group.addAction(action)
        color_menu.addAction(action)

    menu.addSeparator()

    # 시계 숫자에 표시할 도시/시간대 선택 (세그먼트 바는 항상 UTC 기준이라 영향 없음)
    tz_menu = menu.addMenu("Clock Timezone")
    tz_group = QActionGroup(menu)
    tz_group.setExclusive(True)

    def make_tz_selector(tz_name):
        def _select():
            window.set_timezone(tz_name)
        return _select

    for name, tz_name in TIMEZONE_CHOICES:
        action = QAction(name, tz_menu, checkable=True)
        if tz_name == window.display_tz_name:
            action.setChecked(True)
        action.triggered.connect(make_tz_selector(tz_name))
        tz_group.addAction(action)
        tz_menu.addAction(action)

    menu.addSeparator()

    # 시계 숫자 폰트 굵기 선택
    weight_menu = menu.addMenu("Clock Font Weight")
    weight_group = QActionGroup(menu)
    weight_group.setExclusive(True)

    def make_weight_selector(weight_value):
        def _select():
            window.apply_font_weight(weight_value)
        return _select

    for name, weight_value in FONT_WEIGHT_CHOICES:
        action = QAction(name, weight_menu, checkable=True)
        if weight_value == window.font_weight:
            action.setChecked(True)
        action.triggered.connect(make_weight_selector(weight_value))
        weight_group.addAction(action)
        weight_menu.addAction(action)

    menu.addSeparator()

    # ---- 알람 추가 (사용자 지정 시각, 매일 반복 또는 한 번만) ----
    alarm_menu = menu.addMenu("Alarms")

    def prompt_repeat_choice(default_index=0):
        """'Repeat Daily' 또는 'Once' 선택. 취소하면 None 반환."""
        choice, ok = get_choice_input(
            "Repeat", "How should this alarm repeat?",
            ["Repeat Daily", "Once"], default_index
        )
        if not ok:
            return None
        return "daily" if choice == "Repeat Daily" else "once"

    def prompt_add_alarm():
        if len(window.custom_alarms) >= MAX_CUSTOM_ALARMS:
            show_warning(f"You can add up to {MAX_CUSTOM_ALARMS} alarms.")
            return
        text, ok = get_text_input(
            "Add Alarm",
            "Enter a time in HH:MM (24-hour) format (e.g. 07:30)\n"
            "Note: this uses the timezone currently shown on the clock."
        )
        if not ok:
            return
        text = text.strip()
        if not TIME_RE.match(text):
            show_warning("Invalid format. Please enter the time as HH:MM (e.g. 07:30).")
            return

        repeat = prompt_repeat_choice()
        if repeat is None:
            return

        if not window.add_custom_alarm(text, repeat):
            show_info("That time is already used, or the alarm limit has been reached.")

    def prompt_edit_alarm(old_time):
        alarm = window._find_alarm(old_time)
        current_repeat_idx = 1 if (alarm and alarm.get("repeat") == "once") else 0

        text, ok = get_text_input(
            "Edit Alarm", f"Enter a new time for the '{old_time}' alarm (HH:MM):",
            default_text=old_time
        )
        if not ok:
            return
        text = text.strip()
        if not TIME_RE.match(text):
            show_warning("Invalid format. Please enter the time as HH:MM (e.g. 07:30).")
            return

        repeat = prompt_repeat_choice(default_index=current_repeat_idx)
        if repeat is None:
            return

        if not window.edit_custom_alarm(old_time, text, repeat=repeat):
            show_info("That time is already registered.")

    def rebuild_alarm_menu():
        alarm_menu.clear()

        add_action = QAction("+ Add Alarm...", alarm_menu)
        add_action.triggered.connect(prompt_add_alarm)
        alarm_menu.addAction(add_action)

        if window.custom_alarms:
            alarm_menu.addSeparator()
            for a in window.custom_alarms:
                t = a["time"]
                repeat_label = "Daily" if a.get("repeat", "daily") == "daily" else "Once"
                item_menu = alarm_menu.addMenu(f"⏰ {t} ({repeat_label})")

                edit_act = QAction("Edit Time/Repeat", item_menu)
                edit_act.triggered.connect(lambda checked=False, tt=t: prompt_edit_alarm(tt))
                item_menu.addAction(edit_act)

                del_act = QAction("Delete", item_menu)
                del_act.triggered.connect(lambda checked=False, tt=t: window.remove_custom_alarm(tt))
                item_menu.addAction(del_act)
        else:
            empty_action = QAction("No alarms set", alarm_menu)
            empty_action.setEnabled(False)
            alarm_menu.addAction(empty_action)

    rebuild_alarm_menu()
    alarm_menu.aboutToShow.connect(rebuild_alarm_menu)

    menu.addSeparator()

    test_alert = QAction("Test Alert", menu)

    def do_test():
        send_mac_notification("Test Alert", "This is a TradeTime alert test")
        window.trigger_alarm(blink_text=True)

    test_alert.triggered.connect(do_test)
    menu.addAction(test_alert)

    menu.addSeparator()

    donation_action = QAction("❤️ Buy Me a Snack for 🐶 && 🐱", menu)
    donation_action.triggered.connect(lambda: show_donation_dialog(window))
    menu.addAction(donation_action)

    menu.addSeparator()

    quit_action = QAction("Quit", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.show()
    return tray


def main():
    print("TradeTime 시작 중...")
    app = QApplication(sys.argv)
    app.setApplicationName("TradeTime")
    app.setQuitOnLastWindowClosed(True)

    window = ClockWindow()

    screen = app.primaryScreen()
    if screen is not None:
        geo = screen.availableGeometry()
        x = geo.x() + (geo.width() - window.width()) // 2
        y = geo.y() + 40
        window.move(x, y)
    else:
        window.move(100, 100)

    window.show()
    window.raise_()
    window.activateWindow()

    QTimer.singleShot(300, lambda: (window.raise_(), window.activateWindow()))

    tray = build_tray(app, window)  # noqa: F841

    print("실행됨! 화면 위쪽 중앙을 확인하세요.")
    print("종료하려면 이 터미널 창에서 Control(⌃)+C 를 누르세요.")

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
