# ==========================================
# 檔案名稱：gpx_tools.py
# 說明：GPX 路徑工具核心模組，完整比照「暖風 GPX 生成器」的功能設計。
#       刻意不依賴 Tkinter，只用標準函式庫 + math，方便獨立測試。
#
# 內容：
#   - parse_coordinate_text()   多格式座標文字解析（逗號/空格/括號/引號/分號/Plus Code）
#   - decode_plus_code()        Plus Code (Open Location Code) 解碼
#   - nearest_neighbor_order()  最短路徑排序（近鄰貪婪演算法）
#   - apply_endpoint_mode()     路徑終點模式（停在最後/走到中心/走回起點）
#   - apply_coordinate_offset() 座標偏移（標準/大幅度）
#   - generate_circle_path()    繞圈路徑產生器（含第二圈補種偏移角度）
#   - route_stats()             路徑統計（點數/距離/預估時間）
#   - write_gpx() / read_gpx()  GPX 檔案讀寫
#   - write_txt()               純座標文字檔輸出
# ==========================================
from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass
from typing import Optional

LatLng = tuple[float, float]


# ============================================================
# 座標文字解析
# ============================================================

# Plus Code 格式：例如 796RWF8Q+WF 或 8Q7XMW2W+2X
_PLUS_CODE_PATTERN = re.compile(r'^[23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3}$', re.IGNORECASE)

# Open Location Code 使用的 base20 字元表
_OLC_ALPHABET = "23456789CFGHJMPQRVWX"
_OLC_SEPARATOR = "+"
_OLC_PAIR_CODE_LEN = 10
_OLC_GRID_COLUMNS = 4
_OLC_GRID_ROWS = 5
_OLC_LAT_MAX = 90
_OLC_LNG_MAX = 180


def decode_plus_code(code: str) -> Optional[LatLng]:
    """
    解碼 Plus Code (Open Location Code) 成 (緯度, 經度)。
    只支援完整代碼（含區域前綴，例如 796RWF8Q+WF），不支援省略前綴的短碼
    （短碼需要一個參考位置才能還原，這裡沒有提供這個機制）。
    """
    code = code.strip().upper()
    if not _PLUS_CODE_PATTERN.match(code):
        return None

    code = code.replace("+", "")
    lat = -_OLC_LAT_MAX
    lng = -_OLC_LNG_MAX
    # 前 10 碼（5對）的解析度基準值是 20^2=400，不是總範圍(180/360)，
    # 這是 Open Location Code 規格本身的定義：不管緯度還是經度，
    # 起始基準值都一樣是400，每一對字元都先除以20才使用
    lat_resolution = 400.0
    lng_resolution = 400.0

    # 前 10 碼：每 2 碼一組，交替決定緯度/經度區間（base 20）
    pair_len = min(len(code), _OLC_PAIR_CODE_LEN)
    for i in range(0, pair_len, 2):
        lat_resolution /= 20
        lng_resolution /= 20
        lat += _OLC_ALPHABET.index(code[i]) * lat_resolution
        if i + 1 < pair_len:
            lng += _OLC_ALPHABET.index(code[i + 1]) * lng_resolution

    # 剩餘碼：格狀細分（4欄x5列）
    if len(code) > _OLC_PAIR_CODE_LEN:
        for ch in code[_OLC_PAIR_CODE_LEN:]:
            lat_resolution /= _OLC_GRID_ROWS
            lng_resolution /= _OLC_GRID_COLUMNS
            digit = _OLC_ALPHABET.index(ch)
            row = digit // _OLC_GRID_COLUMNS
            col = digit % _OLC_GRID_COLUMNS
            lat += row * lat_resolution
            lng += col * lng_resolution

    # 回傳區間中心點
    return (lat + lat_resolution / 2, lng + lng_resolution / 2)


def encode_plus_code(lat: float, lng: float, code_length: int = 10) -> str:
    """
    把座標編碼成 Plus Code，邏輯跟 decode_plus_code() 互為反函式，
    主要用途是拿來做「編碼再解碼」的自我一致性測試（沒有網路可以對照官方案例時的驗證手段）。
    """
    lat_val = lat + _OLC_LAT_MAX
    lng_val = (lng + _OLC_LNG_MAX) % (_OLC_LNG_MAX * 2)
    if lat_val >= _OLC_LAT_MAX * 2:
        lat_val = _OLC_LAT_MAX * 2 - 1e-9

    code_chars = []
    lat_resolution = 400.0
    lng_resolution = 400.0
    pair_count = min(code_length, _OLC_PAIR_CODE_LEN) // 2

    for _ in range(pair_count):
        lat_resolution /= 20
        lng_resolution /= 20
        lat_digit = min(int(lat_val / lat_resolution), 19)
        lat_val -= lat_digit * lat_resolution
        lng_digit = min(int(lng_val / lng_resolution), 19)
        lng_val -= lng_digit * lng_resolution
        code_chars.append(_OLC_ALPHABET[lat_digit])
        code_chars.append(_OLC_ALPHABET[lng_digit])

    if code_length > _OLC_PAIR_CODE_LEN:
        for _ in range(code_length - _OLC_PAIR_CODE_LEN):
            lat_resolution /= _OLC_GRID_ROWS
            lng_resolution /= _OLC_GRID_COLUMNS
            row = min(int(lat_val / lat_resolution), _OLC_GRID_ROWS - 1)
            lat_val -= row * lat_resolution
            col = min(int(lng_val / lng_resolution), _OLC_GRID_COLUMNS - 1)
            lng_val -= col * lng_resolution
            code_chars.append(_OLC_ALPHABET[row * _OLC_GRID_COLUMNS + col])

    code = "".join(code_chars)
    return code[:8] + "+" + code[8:]


def parse_coordinate_text(text: str) -> list[LatLng]:
    """
    多格式座標文字解析，每行一組，支援：
      - 逗號分隔："25.033, 121.564"
      - 空格分隔："25.033 121.564"
      - 括號包住："(25.033, 121.564)"
      - 引號包住：'"25.033, 121.564"'
      - 分號分隔："25.033; 121.564"
      - Plus Code："796RWF8Q+WF"
    解析不出來的行會直接跳過，不會讓整個匯入失敗。
    """
    results: list[LatLng] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # 去掉括號、引號
        line = line.strip('()[]"\'')

        # 先試 Plus Code
        plus_code_candidate = line.replace(" ", "")
        if _PLUS_CODE_PATTERN.match(plus_code_candidate):
            decoded = decode_plus_code(plus_code_candidate)
            if decoded is not None:
                results.append(decoded)
                continue

        # 統一分隔符號成逗號，再切開
        normalized = re.sub(r'[;，、\s]+', ',', line)
        normalized = normalized.replace("，", ",")
        parts = [p for p in normalized.split(",") if p.strip() != ""]

        if len(parts) >= 2:
            try:
                lat = float(parts[0].strip())
                lng = float(parts[1].strip())
                if -90 <= lat <= 90 and -180 <= lng <= 180:
                    results.append((lat, lng))
            except ValueError:
                continue

    return results


# ============================================================
# 距離計算（跟專案裡其他地方用同一套公式，保持一致）
# ============================================================

def _distance_m(p1: LatLng, p2: LatLng) -> float:
    lat1, lng1 = p1
    lat2, lng2 = p2
    dy = (lat2 - lat1) * 111000.0
    dx = (lng2 - lng1) * 100000.0 * math.cos(math.radians(lat1))
    return math.sqrt(dx * dx + dy * dy)


# ============================================================
# 路徑排序
# ============================================================

def nearest_neighbor_order(points: list[LatLng], start_index: int = 0) -> list[LatLng]:
    """
    最短路徑排序（近鄰貪婪演算法）：從起點開始，每次都走去「目前剩下的點裡最近的那一個」。
    這不保證是數學上絕對最短（真正的最短路徑是 NP-hard 問題），但是業界常用、
    運算快、結果通常已經足夠好的近似解，暖風工具用的也是同類做法。
    """
    if len(points) <= 2:
        return list(points)

    remaining = list(points)
    start_index = max(0, min(start_index, len(remaining) - 1))
    current = remaining.pop(start_index)
    ordered = [current]

    while remaining:
        nearest_idx = min(range(len(remaining)), key=lambda i: _distance_m(current, remaining[i]))
        current = remaining.pop(nearest_idx)
        ordered.append(current)

    return ordered


# ============================================================
# 路徑終點模式
# ============================================================

def apply_endpoint_mode(points: list[LatLng], mode: str = "last") -> list[LatLng]:
    """
    mode:
      "last"     - 停在最後座標，不做任何調整
      "centroid" - 最後多走到所有點的幾何中心
      "loop"     - 最後走回第一個點，形成迴圈
    """
    if not points:
        return points

    result = list(points)
    if mode == "centroid":
        avg_lat = sum(p[0] for p in points) / len(points)
        avg_lng = sum(p[1] for p in points) / len(points)
        result.append((avg_lat, avg_lng))
    elif mode == "loop":
        result.append(points[0])
    return result


# ============================================================
# 座標偏移
# ============================================================

def apply_coordinate_offset(points: list[LatLng], mode: str = "none",
                             rng: Optional[random.Random] = None) -> list[LatLng]:
    """
    mode:
      "none"     - 原始座標，不調整
      "standard" - 每個點隨機偏移 25~55 公尺
      "large"    - 每個點隨機偏移 55~120 公尺（大幅度）
    """
    if mode == "none":
        return list(points)

    rng = rng or random.Random()
    ranges = {"standard": (25.0, 55.0), "large": (55.0, 120.0)}
    min_m, max_m = ranges.get(mode, (0.0, 0.0))

    offset_points = []
    for lat, lng in points:
        distance = rng.uniform(min_m, max_m)
        angle = rng.uniform(0, 2 * math.pi)
        dlat = (distance * math.cos(angle)) / 111000.0
        dlng = (distance * math.sin(angle)) / (100000.0 * math.cos(math.radians(lat)))
        offset_points.append((lat + dlat, lng + dlng))

    return offset_points


# ============================================================
# 繞圈路徑產生器
# ============================================================

def generate_circle_path(
    center: LatLng,
    radius_m: float,
    num_points: int = 12,
    num_laps: int = 1,
    buffer_m: float = 0.0,
    second_lap_offset_deg: float = 0.0,
) -> list[LatLng]:
    """
    針對一個中心點，產生環繞路徑。
    radius_m: 繞圈半徑（含 buffer_m 額外緩衝距離）
    num_points: 每一圈用幾個點來逼近圓形
    num_laps: 繞幾圈（第 2 圈以後會用 second_lap_offset_deg 錯開角度，涵蓋更大範圍）
    second_lap_offset_deg: 第二圈（含之後）相對第一圈的角度偏移
    """
    lat0, lng0 = center
    effective_radius = radius_m + buffer_m
    points: list[LatLng] = []

    for lap in range(max(1, num_laps)):
        angle_offset = math.radians(second_lap_offset_deg) if lap > 0 else 0.0
        for i in range(num_points):
            angle = (2 * math.pi * i / num_points) + angle_offset
            dlat = (effective_radius * math.cos(angle)) / 111000.0
            dlng = (effective_radius * math.sin(angle)) / (100000.0 * math.cos(math.radians(lat0)))
            points.append((lat0 + dlat, lng0 + dlng))

    return points


# ============================================================
# 路徑統計
# ============================================================

@dataclass
class RouteStats:
    point_count: int
    total_distance_m: float
    start: Optional[LatLng]
    end: Optional[LatLng]
    estimated_seconds: float

    @property
    def estimated_time_str(self) -> str:
        total_seconds = int(self.estimated_seconds)
        h, m, s = total_seconds // 3600, (total_seconds % 3600) // 60, total_seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"


def route_stats(points: list[LatLng], speed_kmh: float = 19.0) -> RouteStats:
    """路徑統計：點數、總距離、起訖點、預估時間（預設時速 19 km/h，可自行調整，不寫死）"""
    if not points:
        return RouteStats(0, 0.0, None, None, 0.0)

    total_distance = sum(_distance_m(points[i], points[i + 1]) for i in range(len(points) - 1))
    speed_mps = max(speed_kmh, 0.01) / 3.6
    estimated_seconds = total_distance / speed_mps

    return RouteStats(
        point_count=len(points),
        total_distance_m=total_distance,
        start=points[0],
        end=points[-1],
        estimated_seconds=estimated_seconds,
    )


# ============================================================
# 檔案讀寫
# ============================================================

def write_gpx(points: list[LatLng], file_path: str, name: str = "route") -> None:
    """把座標點寫成標準 GPX 檔案（純字串模板，不需要額外套件）"""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="FakeGpsPatrol-GpxTools">',
        f'  <trk><name>{name}</name><trkseg>',
    ]
    for lat, lng in points:
        lines.append(f'    <trkpt lat="{lat}" lon="{lng}"></trkpt>')
    lines.append('  </trkseg></trk>')
    lines.append('</gpx>')

    with open(file_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def distance_m(p1: LatLng, p2: LatLng) -> float:
    """兩點距離（公尺）。跟 route_timeline()/resample_route() 用同一套換算公式，對外公開版。"""
    return _distance_m(p1, p2)


def resample_route(points: list[LatLng], speed_kmh: float, interval_seconds: float = 1.0) -> list[LatLng]:
    """
    把稀疏的路徑點，依「每隔 interval_seconds 秒移動的距離」內插成密集路徑。

    給「iOS 連續 GPX 播放」巡邏用：pymobiledevice3 的 GPX 播放器只會在兩個相鄰點之間
    睡滿對應秒數、然後瞬間把定位設到下一個點，並不會自己在中間補間畫面。巡邏路徑的
    轉折點（例如弓字型路徑的轉角）之間往往距離很遠，如果直接把這些稀疏轉折點寫進
    GPX，手機在兩個轉角之間會「呆在原地不動一段時間、接著瞬間跳到下一個轉角」，
    看起來還是像用跳的——這就是只做 write_gpx_timed() 還不夠平順的原因。這裡先把路徑
    加密到每約 interval_seconds 秒就有一個點，播放器逐點推進時，手機上的定位才會像
    連續平順移動，而不是長時間靜止後的瞬間移動。
    """
    if len(points) < 2:
        return list(points)
    speed_mps = max(speed_kmh, 0.1) / 3.6
    step_m = max(speed_mps * interval_seconds, 0.5)

    out = [points[0]]
    for i in range(1, len(points)):
        p1, p2 = points[i - 1], points[i]
        seg_dist = _distance_m(p1, p2)
        if seg_dist <= 0:
            continue
        n_steps = max(1, int(seg_dist // step_m))
        for s in range(1, n_steps + 1):
            ratio = min(1.0, (s * step_m) / seg_dist)
            lat = p1[0] + (p2[0] - p1[0]) * ratio
            lng = p1[1] + (p2[1] - p1[1]) * ratio
            out.append((lat, lng))
        if out[-1] != p2:
            out.append(p2)
    return out


def route_timeline(points: list[LatLng], speed_kmh: float) -> list[tuple[float, float, float]]:
    """
    把一串座標換算成 [(累積秒數, lat, lng), ...]，第一筆固定是 (0.0, 起點座標)。

    給「iOS 連續 GPX 播放巡邏」用：寫進 GPX 檔案的 <time> 時間戳記、跟畫面上內插顯示
    目前位置，要用同一套距離/時速換算公式，兩邊進度才會同步一致，見 write_gpx_timed()。
    """
    if not points:
        return []
    speed_mps = max(speed_kmh, 0.1) / 3.6
    timeline = [(0.0, points[0][0], points[0][1])]
    elapsed = 0.0
    for i in range(1, len(points)):
        elapsed += _distance_m(points[i - 1], points[i]) / speed_mps
        timeline.append((elapsed, points[i][0], points[i][1]))
    return timeline


def write_gpx_timed(points: list[LatLng], file_path: str, speed_kmh: float, name: str = "route") -> None:
    """
    寫出「有時間戳記」的 GPX 檔案：每個點的 <time> 依照跟前一點的距離、除以指定時速換算出間隔。

    給 iOS「GPX 路線連續播放」巡邏用——pymobiledevice3 的播放器只有在兩個相鄰點都有
    <time> 標籤時才會照間隔延遲移動；沒有時間戳記的話它完全不管速度、瞬間把整條路線
    送完，手機上的定位還是會「用跳的」，只是把跳動從「逐點重新連線」搬到「GPX 播放」，
    問題並沒有解決。
    """
    from datetime import datetime, timedelta, timezone

    timeline = route_timeline(points, speed_kmh)
    base_time = datetime(2020, 1, 1, tzinfo=timezone.utc)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="FakeGpsPatrol-GpxTools">',
        f'  <trk><name>{name}</name><trkseg>',
    ]
    for elapsed, lat, lng in timeline:
        ts = (base_time + timedelta(seconds=elapsed)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        lines.append(f'    <trkpt lat="{lat}" lon="{lng}"><time>{ts}</time></trkpt>')
    lines.append('  </trkseg></trk>')
    lines.append('</gpx>')

    with open(file_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def read_gpx(file_path: str) -> list[LatLng]:
    """
    讀取 GPX 檔案裡的座標點。優先用 gpxpy（能正確處理各種現實世界的 GPX 格式細節），
    如果環境沒有裝 gpxpy，退回用內建 XML 解析器自己讀 <trkpt>/<rtept>/<wpt>。
    """
    try:
        import gpxpy

        with open(file_path, "r", encoding="utf-8") as f:
            gpx = gpxpy.parse(f)

        points: list[LatLng] = []
        for track in gpx.tracks:
            for segment in track.segments:
                for p in segment.points:
                    points.append((p.latitude, p.longitude))
        if points:
            return points

        for route in gpx.routes:
            for p in route.points:
                points.append((p.latitude, p.longitude))
        if points:
            return points

        for wpt in gpx.waypoints:
            points.append((wpt.latitude, wpt.longitude))
        return points

    except ImportError:
        return _read_gpx_fallback(file_path)


def _read_gpx_fallback(file_path: str) -> list[LatLng]:
    """沒有 gpxpy 時的備援讀取方式：用標準函式庫的 XML 解析器"""
    import xml.etree.ElementTree as ET

    tree = ET.parse(file_path)
    root = tree.getroot()

    # GPX 檔案通常帶命名空間，找標籤時要去掉命名空間前綴比對
    def local_tag(elem):
        return elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag

    for tag_name in ("trkpt", "rtept", "wpt"):
        points = []
        for elem in root.iter():
            if local_tag(elem) == tag_name:
                lat = elem.get("lat")
                lon = elem.get("lon")
                if lat is not None and lon is not None:
                    points.append((float(lat), float(lon)))
        if points:
            return points

    return []


def write_txt(points: list[LatLng], file_path: str) -> None:
    """純座標文字檔輸出，每行一組「緯度,經度」"""
    with open(file_path, "w", encoding="utf-8") as f:
        for lat, lng in points:
            f.write(f"{lat},{lng}\n")