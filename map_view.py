# ==========================================
# 檔案名稱：map_view.py
# 說明：PySide6 版地圖元件，取代 Tkinter 版用的 tkintermapview。
#       用 QWebEngineView 內嵌 Leaflet.js，Python <-> JS 用 QWebChannel 溝通。
#
# 對應 Tkinter 版的功能：
#   - 右鍵選單：設定此處為定位點 / 新增多邊形頂點 / GPX工具新增此點
#   - 標記（目前位置、巡邏路徑起點...）
#   - 路徑線（弓字型巡邏路線）、多邊形（畫巡邏區域）
#   - 圖磚離線快取：透過 tile_cache_server.py 起的本機代理伺服器
# ==========================================
from __future__ import annotations

import json
import os

from PySide6.QtCore import QObject, Signal, Slot, QUrl
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView

_HTML_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map_page.html")


class MapBridge(QObject):
    """JS 端透過 QWebChannel 呼叫這個物件的 slot；Python 端監聽對應的 signal。"""

    # 右鍵選單三個動作，統一用同一個 signal 送出 (action, lat, lng)
    contextAction = Signal(str, float, float)
    # 地圖初始化完成（Leaflet 物件已經 ready，Python 端可以開始下指令）才是安全的
    mapReady = Signal()
    # GPX 點在地圖上被拖曳/刪除（index 對應 Python 端 gpx_points 清單的索引）
    gpxPointMoved = Signal(int, float, float)
    gpxPointDeleted = Signal(int)

    @Slot(str, float, float)
    def onContextAction(self, action: str, lat: float, lng: float) -> None:
        self.contextAction.emit(action, lat, lng)

    @Slot()
    def onMapReady(self) -> None:
        self.mapReady.emit()

    @Slot(int, float, float)
    def onGpxPointMoved(self, index: int, lat: float, lng: float) -> None:
        self.gpxPointMoved.emit(index, lat, lng)

    @Slot(int)
    def onGpxPointDeleted(self, index: int) -> None:
        self.gpxPointDeleted.emit(index)


_PAGE_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<title>map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<style>
  html, body, #map { height: 100%; margin: 0; padding: 0; background: #121212; }
  .gold-ctx-menu {
    position: absolute; z-index: 9999; background: #1e1e1e; border: 1px solid #c5a059;
    border-radius: 4px; font-family: "Microsoft JhengHei", sans-serif; font-size: 13px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.6); overflow: hidden;
  }
  .gold-ctx-menu div { padding: 8px 14px; color: #e0e0e0; cursor: pointer; white-space: nowrap; }
  .gold-ctx-menu div:hover { background: #ffd700; color: #121212; }
</style>
</head>
<body>
<div id="map"></div>
<script>
  var TILE_URL = "__TILE_URL__";
  var map = L.map('map', { zoomControl: true, attributionControl: false }).setView([25.033964, 121.564468], 16);
  L.tileLayer(TILE_URL, { maxZoom: 20 }).addTo(map);

  var markers = {};   // id -> L.marker
  var paths = {};     // id -> L.polyline
  var polygons = {};  // id -> L.polygon
  var gpxMarkers = []; // GPX 路徑工具用的可拖曳/可刪除數字標記，索引對應 Python 端 gpx_points

  var bridge = null;
  new QWebChannel(qt.webChannelTransport, function(channel) {
    bridge = channel.objects.bridge;
    bridge.onMapReady();
  });

  // ---------------- 右鍵選單（對應 Tkinter 版 add_right_click_menu_command） ----------------
  var ctxMenuEl = null;
  function closeCtxMenu() {
    if (ctxMenuEl) { ctxMenuEl.remove(); ctxMenuEl = null; }
  }
  map.on('contextmenu', function(e) {
    closeCtxMenu();
    var lat = e.latlng.lat, lng = e.latlng.lng;
    var items = [
      ["set_location", "📍 設定此處為定位點"],
      ["add_polygon_vertex", "🔺 新增多邊形頂點"],
      ["gpx_add_point", "🗺️ GPX工具：新增此點"],
      ["add_favorite", "⭐ 加入收藏"],
    ];
    var menu = document.createElement('div');
    menu.className = 'gold-ctx-menu';
    menu.style.left = e.containerPoint.x + 'px';
    menu.style.top = e.containerPoint.y + 'px';
    items.forEach(function(item) {
      var d = document.createElement('div');
      d.innerText = item[1];
      d.onclick = function() {
        closeCtxMenu();
        if (bridge) bridge.onContextAction(item[0], lat, lng);
      };
      menu.appendChild(d);
    });
    map.getContainer().appendChild(menu);
    ctxMenuEl = menu;
  });
  map.on('click', closeCtxMenu);
  map.on('movestart', closeCtxMenu);

  // ---------------- 給 Python 呼叫的 API ----------------
  window.mapApi = {
    setPosition: function(lat, lng, zoom) {
      if (zoom !== undefined && zoom !== null) { map.setView([lat, lng], zoom); }
      else { map.panTo([lat, lng]); }
    },
    setMarker: function(id, lat, lng, color, label) {
      if (markers[id]) { map.removeLayer(markers[id]); }
      var icon = L.divIcon({
        className: '',
        html: '<div style="background:' + color + ';width:14px;height:14px;border-radius:50%;' +
              'border:2px solid #121212;box-shadow:0 0 4px rgba(0,0,0,.6);"></div>' +
              (label ? '<div style="margin-top:2px;color:' + color + ';font-weight:bold;' +
               'font-size:11px;text-shadow:0 0 3px #000,0 0 3px #000;white-space:nowrap;">' + label + '</div>' : ''),
        iconSize: [14, 14], iconAnchor: [7, 7],
      });
      markers[id] = L.marker([lat, lng], { icon: icon }).addTo(map);
    },
    removeMarker: function(id) {
      if (markers[id]) { map.removeLayer(markers[id]); delete markers[id]; }
    },
    clearMarkersByPrefix: function(prefix) {
      Object.keys(markers).forEach(function(id) {
        if (id.indexOf(prefix) === 0) { map.removeLayer(markers[id]); delete markers[id]; }
      });
    },
    setPath: function(id, latlngs, color) {
      if (paths[id]) { map.removeLayer(paths[id]); }
      paths[id] = L.polyline(latlngs, { color: color || '#ffd700', weight: 4 }).addTo(map);
    },
    removePath: function(id) {
      if (paths[id]) { map.removeLayer(paths[id]); delete paths[id]; }
    },
    clearPathsByPrefix: function(prefix) {
      Object.keys(paths).forEach(function(id) {
        if (id.indexOf(prefix) === 0) { map.removeLayer(paths[id]); delete paths[id]; }
      });
    },
    setPolygon: function(id, latlngs, color) {
      if (polygons[id]) { map.removeLayer(polygons[id]); }
      polygons[id] = L.polygon(latlngs, { color: color || '#3498db', weight: 2, fillOpacity: 0.15 }).addTo(map);
    },
    removePolygon: function(id) {
      if (polygons[id]) { map.removeLayer(polygons[id]); delete polygons[id]; }
    },
    setGpxPoints: function(points) {
      // 每次整批重畫：GPX 點的索引在新增/刪除/排序後都會變，與其一個一個對應
      // 更新，不如直接全部清掉重畫，邏輯簡單很多，且點數通常不多（幾十到
      // 百來個），效能上沒有顧慮
      gpxMarkers.forEach(function(m) { map.removeLayer(m); });
      gpxMarkers = [];
      points.forEach(function(p, idx) {
        var icon = L.divIcon({
          className: '',
          html: '<div style="background:#ffd700;color:#121212;width:20px;height:20px;' +
                'border-radius:50%;border:2px solid #121212;display:flex;align-items:center;' +
                'justify-content:center;font-size:10px;font-weight:bold;cursor:move;">' +
                (idx + 1) + '</div>',
          iconSize: [20, 20], iconAnchor: [10, 10],
        });
        var marker = L.marker([p[0], p[1]], { icon: icon, draggable: true }).addTo(map);
        marker.on('dragend', function(e) {
          var ll = e.target.getLatLng();
          if (bridge) bridge.onGpxPointMoved(idx, ll.lat, ll.lng);
        });
        marker.on('contextmenu', function(e) {
          L.DomEvent.stopPropagation(e);
          if (bridge) bridge.onGpxPointDeleted(idx);
        });
        gpxMarkers.push(marker);
      });
    },
    clearGpxPoints: function() {
      gpxMarkers.forEach(function(m) { map.removeLayer(m); });
      gpxMarkers = [];
    },
    clearPolygonsByPrefix: function(prefix) {
      Object.keys(polygons).forEach(function(id) {
        if (id.indexOf(prefix) === 0) { map.removeLayer(polygons[id]); delete polygons[id]; }
      });
    },
  };
</script>
</body>
</html>
"""


class MapView(QWebEngineView):
    """
    包裝 QWebEngineView + Leaflet + QWebChannel，對外提供跟 tkintermapview 相近的介面。
    地圖尚未載入完成前呼叫 set_marker 等方法會被安全忽略（由 self._ready 控制）；
    呼叫端如果需要保證指令一定生效，請接 mapReady signal 之後才開始下第一批指令。
    """

    def __init__(self, tile_url: str, parent=None):
        super().__init__(parent)
        self._ready = False
        self._pending: list[str] = []

        self.bridge = MapBridge(self)
        self.bridge.mapReady.connect(self._on_ready)

        self.channel = QWebChannel(self.page())
        self.channel.registerObject("bridge", self.bridge)
        self.page().setWebChannel(self.channel)

        # base URL 一定要用 http（不能用 https）：圖磚代理伺服器只有 http，
        # 如果頁面本身是 https，瀏覽器的 Mixed Content 政策會直接擋掉所有 http 圖磚請求，
        # 地圖會整張空白（這是實測到的真實 bug，不是理論上的假設）。
        html = _PAGE_HTML.replace("__TILE_URL__", tile_url)
        self.setHtml(html, QUrl("http://local.map/"))

    def _on_ready(self) -> None:
        self._ready = True
        for js in self._pending:
            self.page().runJavaScript(js)
        self._pending.clear()

    def _run(self, js: str) -> None:
        if self._ready:
            self.page().runJavaScript(js)
        else:
            self._pending.append(js)

    # ---------------- 對外 API ----------------
    def set_position(self, lat: float, lng: float, zoom: int | None = None) -> None:
        zoom_js = "null" if zoom is None else str(zoom)
        self._run(f"window.mapApi.setPosition({lat!r}, {lng!r}, {zoom_js});")

    def set_marker(self, marker_id: str, lat: float, lng: float, color: str = "#2ecc71", label: str = "") -> None:
        self._run(f"window.mapApi.setMarker({marker_id!r}, {lat!r}, {lng!r}, {color!r}, {label!r});")

    def remove_marker(self, marker_id: str) -> None:
        self._run(f"window.mapApi.removeMarker({marker_id!r});")

    def clear_markers_by_prefix(self, prefix: str) -> None:
        self._run(f"window.mapApi.clearMarkersByPrefix({prefix!r});")

    def set_path(self, path_id: str, points: list[tuple[float, float]], color: str = "#ffd700") -> None:
        latlngs = json.dumps([[p[0], p[1]] for p in points])
        self._run(f"window.mapApi.setPath({path_id!r}, {latlngs}, {color!r});")

    def remove_path(self, path_id: str) -> None:
        self._run(f"window.mapApi.removePath({path_id!r});")

    def clear_paths_by_prefix(self, prefix: str) -> None:
        self._run(f"window.mapApi.clearPathsByPrefix({prefix!r});")

    def set_polygon(self, poly_id: str, points: list[tuple[float, float]], color: str = "#3498db") -> None:
        latlngs = json.dumps([[p[0], p[1]] for p in points])
        self._run(f"window.mapApi.setPolygon({poly_id!r}, {latlngs}, {color!r});")

    def remove_polygon(self, poly_id: str) -> None:
        self._run(f"window.mapApi.removePolygon({poly_id!r});")

    def clear_polygons_by_prefix(self, prefix: str) -> None:
        self._run(f"window.mapApi.clearPolygonsByPrefix({prefix!r});")

    def set_gpx_points(self, points: list[tuple[float, float]]) -> None:
        """畫出 GPX 路徑工具目前的座標點，每個點都可以在地圖上直接拖曳調整、右鍵刪除"""
        latlngs = json.dumps([[p[0], p[1]] for p in points])
        self._run(f"window.mapApi.setGpxPoints({latlngs});")

    def clear_gpx_points(self) -> None:
        self._run("window.mapApi.clearGpxPoints();")
