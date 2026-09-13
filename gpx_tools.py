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
# 經度正規化（跨 ±180 換日線）
# ============================================================
#
# 經度是環狀的：+180 跟 -180 是同一條線。直接對經度做加減／內插，在換日線附近
# 會算出「繞地球一圈」的荒謬結果——例如從 179.9 走到 -179.9，實際上只是往東
# 跨 0.2 度，但直接相減會得到 -359.8 度，距離、內插、排序全部會錯。
# 所有「經度加減」跟「兩經度相差多少」都要透過下面兩個函式，不要直接用 + - 。

def normalize_lng(lng: float) -> float:
    """把經度收斂回 [-180, 180)。超過 +180 就從 -180 那一側繞回來，反之亦然。"""
    return (lng + 180.0) % 360.0 - 180.0


def lng_delta(from_lng: float, to_lng: float) -> float:
    """兩個經度之間「走最短那一邊」的有號差值，範圍 [-180, 180)。
    例：lng_delta(179.9, -179.9) == 0.2（往東跨換日線），而不是 -359.8。"""
    return normalize_lng(to_lng - from_lng)


def normalize_point(point: LatLng) -> LatLng:
    return (point[0], normalize_lng(point[1]))


# ============================================================
# 距離計算（跟專案裡其他地方用同一套公式，保持一致）
# ============================================================

def _distance_m(p1: LatLng, p2: LatLng) -> float:
    lat1, lng1 = p1
    lat2, lng2 = p2
    dy = (lat2 - lat1) * 111000.0
    dx = lng_delta(lng1, lng2) * 100000.0 * math.cos(math.radians(lat1))
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
        # 經度不能直接平均：179 跟 -179 這種跨換日線的組合，直接平均會得到 0
        # （地球另一邊）。改成把每個經度當成單位圓上的向量取平均角度（環狀平均），
        # 不跨換日線時結果跟直接平均一樣。
        sin_sum = sum(math.sin(math.radians(p[1])) for p in points)
        cos_sum = sum(math.cos(math.radians(p[1])) for p in points)
        if abs(sin_sum) < 1e-12 and abs(cos_sum) < 1e-12:
            avg_lng = points[0][1]  # 所有點剛好平均掉（例如正好對蹠），退回用第一點
        else:
            avg_lng = normalize_lng(math.degrees(math.atan2(sin_sum, cos_sum)))
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
        offset_points.append((lat + dlat, normalize_lng(lng + dlng)))

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
            points.append((lat0 + dlat, normalize_lng(lng0 + dlng)))

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
        dlng = lng_delta(p1[1], p2[1])
        for s in range(1, n_steps + 1):
            ratio = min(1.0, (s * step_m) / seg_dist)
            lat = p1[0] + (p2[0] - p1[0]) * ratio
            lng = normalize_lng(p1[1] + dlng * ratio)
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


# ============================================================
# 其他格式匯入（CSV / JSON）
# ============================================================

_NUMBER_PATTERN = re.compile(r'(?<![A-Za-z0-9])-?\d+(?:\.\d+)?(?![A-Za-z0-9])')


def _first_two_numbers_in_line(line: str) -> Optional[LatLng]:
    """
    在一行文字裡找「前兩個帶正負號的數字」當 lat/lon，不管它們前面有沒有名稱、
    序號之類的非數字欄位（真實世界的 CSV 常常是「名稱,緯度,經度」或有表頭列，
    嚴格假設「第一、第二個逗號分隔欄位就是座標」反而很容易漏掉——實測過確實
    會漏掉「名稱,25.0,121.5」這種常見格式）。
    """
    matches = _NUMBER_PATTERN.findall(line)
    if len(matches) < 2:
        return None
    try:
        lat, lng = float(matches[0]), float(matches[1])
    except ValueError:
        return None
    if -90 <= lat <= 90 and -180 <= lng <= 180:
        return (lat, lng)
    return None


def read_csv(file_path: str) -> list[LatLng]:
    """
    從 CSV 檔案粗略擷取座標：每行找出前兩個數字當 lat/lon，不要求嚴格的欄位對應
    （不同來源的 CSV 欄位順序、有沒有表頭、有沒有名稱欄位都不一樣，嚴格解析
    反而容易失敗；跟 FreeWay 規格書 §14.2 描述的「用正規表示式擷取前兩個數字」
    同一種策略）。表頭列（例如「name,lat,lng」）沒有兩個看起來像座標的數字，
    會被自然跳過，不用另外偵測有沒有表頭。
    """
    with open(file_path, "r", encoding="utf-8-sig", errors="replace") as f:
        lines = f.read().splitlines()
    points: list[LatLng] = []
    for line in lines:
        point = _first_two_numbers_in_line(line)
        if point is not None:
            points.append(point)
    return points


def _scan_text_for_coordinates(text: str) -> list[LatLng]:
    """逐行掃描任意文字，每行找前兩個數字當座標——跟 read_csv 同一套邏輯，用在
    JSON 解析失敗時的最後手段，不用 parse_coordinate_text()（那個函式假設座標是
    「該行第一、二個逗號分隔欄位」，遇到座標前面還有其他文字就會漏掉，不適合
    這裡「格式完全不明」的情境）。"""
    points: list[LatLng] = []
    for line in text.splitlines():
        point = _first_two_numbers_in_line(line)
        if point is not None:
            points.append(point)
    return points


def read_json(file_path: str) -> list[LatLng]:
    """
    從 JSON 檔案擷取座標，接受兩種常見格式：
      [[lat, lon], [lat, lon], ...]
      [{"lat": .., "lng"/"lon": ..}, ...]
    格式不符或解析失敗時，退回逐行掃描文字裡的數字當最後手段，不直接判失敗。
    """
    import json as _json

    with open(file_path, "r", encoding="utf-8-sig", errors="replace") as f:
        raw = f.read()

    try:
        data = _json.loads(raw)
    except Exception:
        return _scan_text_for_coordinates(raw)

    points: list[LatLng] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    lat, lng = float(item[0]), float(item[1])
                except (TypeError, ValueError):
                    continue
                if -90 <= lat <= 90 and -180 <= lng <= 180:
                    points.append((lat, lng))
            elif isinstance(item, dict):
                lat = item.get("lat")
                lng = item.get("lng", item.get("lon"))
                if lat is None or lng is None:
                    continue
                try:
                    lat, lng = float(lat), float(lng)
                except (TypeError, ValueError):
                    continue
                if -90 <= lat <= 90 and -180 <= lng <= 180:
                    points.append((lat, lng))

    return points if points else _scan_text_for_coordinates(raw)


# ============================================================
# 路線最佳化（開放路徑近似最短解：nearest-neighbor + 2-opt + 片段搬移）
# ============================================================

def optimize_route(points: list[LatLng]) -> list[LatLng]:
    """
    比 nearest_neighbor_order() 更講究的路線最佳化，邏輯照 FreeWay 規格書 §7：

      候選起點數 = min(n, 150)
      對每個候選起點：
        1. nearest-neighbor 建立初始路徑
        2. 最多 30 個改善回合，每回合嘗試：
           - 所有 2-opt 區段反轉
           - 長度 1/2/3 片段搬移（正向、反向插入都比較）
        3. 該回合完全沒有改善就提前停止
      回傳所有候選裡總長度最短的路徑

    這是啟發式近似解，不保證全域最佳解（真正最短路徑是 NP-hard 問題）。內部用距離的
    「增減量」而不是每次重算整條路徑長度，才有辦法在合理時間內跑完（點數多時仍可能較慢，
    呼叫端如果用在幾百個點以上的路線建議自行評估耗時）。
    """
    n = len(points)
    if n < 3:
        return list(points)

    dist = [[_distance_m(points[i], points[j]) for j in range(n)] for i in range(n)]

    def path_length(order: list[int]) -> float:
        return sum(dist[order[k]][order[k + 1]] for k in range(len(order) - 1))

    def nearest_neighbor_from(start: int) -> list[int]:
        remaining = set(range(n))
        remaining.discard(start)
        order = [start]
        current = start
        while remaining:
            nxt = min(remaining, key=lambda i: dist[current][i])
            order.append(nxt)
            remaining.discard(nxt)
            current = nxt
        return order

    def two_opt_pass(order: list[int]) -> tuple[list[int], bool]:
        improved = False
        length = len(order)
        for i in range(length - 2):
            a, b = order[i], order[i + 1]
            for j in range(i + 2, length - 1):
                c, d = order[j], order[j + 1]
                delta = (dist[a][c] + dist[b][d]) - (dist[a][b] + dist[c][d])
                if delta < -1e-9:
                    order[i + 1:j + 1] = reversed(order[i + 1:j + 1])
                    improved = True
                    b = order[i + 1]
        return order, improved

    def relocation_pass(order: list[int]) -> tuple[list[int], bool]:
        improved = False
        for seg_len in (1, 2, 3):
            i = 0
            while i + seg_len <= len(order):
                n_order = len(order)
                removal_gain = 0.0
                if i > 0:
                    removal_gain += dist[order[i - 1]][order[i]]
                if i + seg_len < n_order:
                    removal_gain += dist[order[i + seg_len - 1]][order[i + seg_len]]
                if i > 0 and i + seg_len < n_order:
                    removal_gain -= dist[order[i - 1]][order[i + seg_len]]

                segment = order[i:i + seg_len]
                rest = order[:i] + order[i + seg_len:]
                best_gain = 1e-9
                best_candidate = None
                variants = (segment,) if seg_len == 1 else (segment, list(reversed(segment)))
                for insert_at in range(len(rest) + 1):
                    for seg in variants:
                        first, last = seg[0], seg[-1]
                        cost = 0.0
                        if insert_at > 0:
                            cost += dist[rest[insert_at - 1]][first]
                        if insert_at < len(rest):
                            cost += dist[last][rest[insert_at]]
                        if 0 < insert_at < len(rest):
                            cost -= dist[rest[insert_at - 1]][rest[insert_at]]
                        net_gain = removal_gain - cost
                        if net_gain > best_gain:
                            best_gain = net_gain
                            best_candidate = rest[:insert_at] + seg + rest[insert_at:]
                if best_candidate is not None:
                    order = best_candidate
                    improved = True
                    continue  # order 變了，同一個 i 重新檢查一次
                i += 1
        return order, improved

    candidate_starts = range(n) if n <= 150 else range(150)
    best_order: Optional[list[int]] = None
    best_length = float("inf")

    for start in candidate_starts:
        order = nearest_neighbor_from(start)
        for _round in range(30):
            order, improved_2opt = two_opt_pass(order)
            order, improved_reloc = relocation_pass(order)
            if not improved_2opt and not improved_reloc:
                break
        length = path_length(order)
        if length < best_length:
            best_length = length
            best_order = order

    return [points[i] for i in best_order]


# ============================================================
# RouteStep：帶等待時間的路徑點（種花路徑／跳躍路徑專用）
# ============================================================

@dataclass
class RouteStep:
    """
    帶等待時間的路徑點。跟純座標的 LatLng 不同，這裡多了 wait_s——代表「移動到這個點之後，
    停留這麼多秒再繼續下一步」，種花路徑（圈的起訖點）跟跳躍路徑（到點/出發前）都需要這個
    概念，一般路線／Z字巡邏用不到，繼續用純座標清單就好，不用套用這個模型。
    """
    lat: float
    lng: float
    wait_s: float = 0.0


def _linear_walk_steps(
    from_point: LatLng, to_point: LatLng, speed_kmh: float
) -> list[RouteStep]:
    """兩點間用線性插值切成多個中間步驟，公式跟既有一般連續移動(§6.5)同一套 tick 邏輯。"""
    distance = _distance_m(from_point, to_point)
    speed_mps = max(0.35, speed_kmh * 1000.0 / 3600.0 * 0.5)
    steps_count = max(1, min(5000, math.ceil(distance / speed_mps))) if distance > 0 else 0

    out: list[RouteStep] = []
    from_lat, from_lng = from_point
    to_lat, to_lng = to_point
    dlng = lng_delta(from_lng, to_lng)
    for i in range(1, steps_count + 1):
        ratio = i / steps_count
        out.append(RouteStep(
            lat=from_lat + (to_lat - from_lat) * ratio,
            lng=normalize_lng(from_lng + dlng * ratio),
        ))
    return out


# ============================================================
# 種花路徑生成
# ============================================================

def flower_circle_points(
    center: LatLng,
    radius_m: float,
    turns: float,
    speed_kmh: float,
    arrival_wait: float,
    departure_wait: float,
) -> list[RouteStep]:
    """
    對一個花點中心產生繞圈路徑，附帶等待時間。圓周座標公式、取樣點數公式都照 FreeWay
    規格書 §8.2/§8.3 原樣實作（含 111320 這個比我們專案別處常用的 111000 更精確的地球
    半徑常數，僅限這個函式使用，不影響 _distance_m 等既有共用函式，避免動到其他既有
    功能的行為）。第一點套 arrival_wait，最後一點套 departure_wait。
    """
    lat0, lng0 = center
    speed_mps = max(0.35, speed_kmh * 1000.0 / 3600.0)
    circle_distance = 2 * math.pi * radius_m * max(turns, 1e-6)
    steps = max(12, min(5000, math.ceil(circle_distance / (speed_mps * 0.5))))

    total_angle = 2 * math.pi * turns
    out: list[RouteStep] = []
    for i in range(steps):
        angle = total_angle * i / (steps - 1) if steps > 1 else 0.0
        dlat = (radius_m / 111320.0) * math.cos(angle)
        dlng = (radius_m / (111320.0 * max(0.1, math.cos(math.radians(lat0))))) * math.sin(angle)
        wait_s = 0.0
        if i == 0:
            wait_s = arrival_wait
        elif i == steps - 1:
            wait_s = departure_wait
        out.append(RouteStep(lat=lat0 + dlat, lng=normalize_lng(lng0 + dlng), wait_s=wait_s))
    return out


def build_flower_route_detailed(
    centers: list[LatLng],
    sort_mode: str,
    plant_mode: str,
    ring1: dict,
    ring2: Optional[dict],
    speed_kmh: float,
) -> tuple[list[RouteStep], list[tuple[int, int, int]]]:
    """
    跟 build_flower_route() 同一套產生邏輯，但額外回傳「同步點」清單，
    給團體種花用：每跑完一個花點的一圈就是一個同步點，房主要在這裡等團員跟上。

    回傳 (steps, boundaries)，boundaries 是 [(steps 的索引, 第幾圈, 第幾個花點), ...]，
    索引指的是「走完這一步就抵達同步點」，圈從 1 起算、花點從 0 起算
    （對應規格書 §11.8 boundary 公式裡的 ring 與 center）。
    """
    if not centers:
        return [], []
    ordered_centers = optimize_route(centers) if sort_mode == "shortest" else list(centers)

    steps: list[RouteStep] = []
    boundaries: list[tuple[int, int, int]] = []

    def append_ring(ring_settings: dict, ring_no: int) -> None:
        for center_idx, center in enumerate(ordered_centers):
            ring_steps = flower_circle_points(
                center,
                ring_settings["radius"],
                ring_settings["turns"],
                speed_kmh,
                ring_settings["arrival_wait"],
                ring_settings["departure_wait"],
            )
            if not ring_steps:
                continue
            if plant_mode == "walk" and steps:
                last = steps[-1]
                steps.extend(_linear_walk_steps((last.lat, last.lng), (ring_steps[0].lat, ring_steps[0].lng), speed_kmh))
            steps.extend(ring_steps)
            boundaries.append((len(steps) - 1, ring_no, center_idx))

    append_ring(ring1, 1)
    if ring2:
        append_ring(ring2, 2)

    return steps, boundaries


def build_flower_route(
    centers: list[LatLng],
    sort_mode: str,
    plant_mode: str,
    ring1: dict,
    ring2: Optional[dict],
    speed_kmh: float,
) -> list[RouteStep]:
    """
    串接多個花點的種花路徑。ring1/ring2 格式：
      {"radius": float, "turns": float, "arrival_wait": float, "departure_wait": float}
    ring2 傳 None 代表不啟用第二圈。
    sort_mode: "shortest"（呼叫 optimize_route）／"paste"（貼上原順序）。
    plant_mode: "walk"（花點之間直線走過去）／"teleport"（直接瞬移到下一花點起點）。
    執行順序照規格書 §8.4：第一圈全部花點跑完，才進第二圈全部花點（若啟用）。
    """
    return build_flower_route_detailed(centers, sort_mode, plant_mode, ring1, ring2, speed_kmh)[0]


# ============================================================
# 跳躍路徑生成
# ============================================================

def build_jump_route(
    points: list[LatLng],
    before_wait: float,
    after_wait: float,
    forward_m: float,
) -> list[RouteStep]:
    """
    跳躍路徑，照規格書 §9.2：
      瞬移到目標點 → 到達後等待 after_wait 秒 → 朝下一個目標方向走 forward_m 公尺
      → 等待 before_wait 秒 → 瞬移到下一個目標
    最後一個點不用再往「下一個」方向走，只停留 after_wait。
    """
    if not points:
        return []
    steps: list[RouteStep] = []
    n = len(points)
    for idx, (lat, lng) in enumerate(points):
        steps.append(RouteStep(lat=lat, lng=lng, wait_s=after_wait))
        if idx < n - 1 and forward_m > 0:
            next_lat, next_lng = points[idx + 1]
            distance = _distance_m((lat, lng), (next_lat, next_lng))
            if distance > 0:
                ratio = min(1.0, forward_m / distance)
                steps.append(RouteStep(
                    lat=lat + (next_lat - lat) * ratio,
                    lng=normalize_lng(lng + lng_delta(lng, next_lng) * ratio),
                    wait_s=before_wait,
                ))
    return steps


# ============================================================
# 同心圓生成
# ============================================================

def generate_concentric_circles(
    center: LatLng,
    start_radius: float,
    ring_count: int,
    radius_step: float,
    points_per_ring: int,
) -> list[LatLng]:
    """
    由圓心向外生成多層同心圓，每圈半徑遞增 radius_step，各圈平均分布 points_per_ring 個點。
    半徑跟每圈間距低於 25 公尺時強制拉到 25 公尺（照規格書 §10.2，降低相鄰路線過度重疊）。
    回傳純座標清單，沿用既有 PatrolWorker.set_segments() 執行路徑即可，不需要 RouteStep。
    """
    start_radius = max(25.0, start_radius)
    radius_step = max(25.0, radius_step)
    points_per_ring = max(4, points_per_ring)

    lat0, lng0 = center
    result: list[LatLng] = []
    for ring in range(max(1, ring_count)):
        radius = start_radius + ring * radius_step
        for i in range(points_per_ring):
            angle = 2 * math.pi * i / points_per_ring
            dlat = (radius / 111320.0) * math.cos(angle)
            dlng = (radius / (111320.0 * max(0.1, math.cos(math.radians(lat0))))) * math.sin(angle)
            result.append((lat0 + dlat, normalize_lng(lng0 + dlng)))
    return result


# ============================================================
# RouteStep 時間軸／GPX 匯出（種花／跳躍路徑專用，含停留等待）
# ============================================================
#
# route_timeline()/resample_route()/write_gpx_timed() 都是「純距離／時速換算時間」的
# 模型，沒有辦法表示「在某個點停留 N 秒再走」——兩個座標相同的點之間距離是 0，換算出
# 來的時間增量也會是 0，不會產生真正的停留。RouteStep 需要一套會把 wait_s 算進時間軸
# 的獨立函式，不能沿用上面那一套。

def route_step_timeline(steps: list[RouteStep], speed_kmh: float) -> list[tuple[float, float, float]]:
    """
    把一串 RouteStep 換算成 [(累積秒數, lat, lng), ...]。移動距離部分跟 route_timeline()
    同一套「距離/時速」邏輯；每個 RouteStep 的 wait_s 會在該點的座標上多插入一個「等待
    結束」的時間點，兩個時間點座標相同、但時間不同，重播時就會在那個點真正停留。

    種花／跳躍路徑產生器本身在轉折/繞圈處已經有夠密的取樣點（見 flower_circle_points()／
    _linear_walk_steps()），不需要再另外呼叫 resample_route() 加密。
    """
    if not steps:
        return []
    speed_mps = max(speed_kmh, 0.1) / 3.6

    timeline: list[tuple[float, float, float]] = [(0.0, steps[0].lat, steps[0].lng)]
    elapsed = 0.0
    if steps[0].wait_s > 0:
        elapsed += steps[0].wait_s
        timeline.append((elapsed, steps[0].lat, steps[0].lng))

    for i in range(1, len(steps)):
        prev, cur = steps[i - 1], steps[i]
        elapsed += _distance_m((prev.lat, prev.lng), (cur.lat, cur.lng)) / speed_mps
        timeline.append((elapsed, cur.lat, cur.lng))
        if cur.wait_s > 0:
            elapsed += cur.wait_s
            timeline.append((elapsed, cur.lat, cur.lng))

    return timeline


def write_gpx_timed_from_steps(
    steps: list[RouteStep], file_path: str, speed_kmh: float, name: str = "route"
) -> None:
    """跟 write_gpx_timed() 一樣輸出帶 <time> 的 GPX 檔，但時間軸改用 route_step_timeline()
    算出（含每個點的停留等待），給 iOS 連續播放種花／跳躍路徑用。"""
    from datetime import datetime, timedelta, timezone

    timeline = route_step_timeline(steps, speed_kmh)
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