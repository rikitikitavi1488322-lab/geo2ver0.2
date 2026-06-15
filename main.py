from PIL import Image, ImageFilter
import random
import json
import os
import raspred
import clas

import georef
geo_system = georef.GeoReferencer()
control_points = []

import numpy as np
elevation_matrix = None

status_message = "Система готова"
karta = 'geo2.jpg'

sample_processed = None
img_lab_global = None   # LAB-матрица всего изображения (H, W, 3) — основа всех операций

# Настройки вычислений (изменяются через интерфейс)
delta_e_tolerance = 20  # Порог Delta-E для сегментации по цвету легенды
blur_radius = 1         # Лёгкое размытие убирает JPEG-артефакты, не трогая границы
watershed_min_dist = 50 # Минимальное расстояние между центрами зон Watershed (в пикселях)

# Устаревшие настройки K-Means — оставлены для совместимости при загрузке старых проектов
k_clusters = 20
min_d2 = 500

slovar_pix = {}
slovar_pix_lab = {}
list_of_rgb = []
list_of_lab = []
sp_sloy = []

x = 0
y = 0
pixels = None


# ─────────────────────────────────────────────
#  Конвертация цветов (без внешних зависимостей)
# ─────────────────────────────────────────────

def rgb2lab(rgb_array):
    """RGB uint8 массив (H,W,3) → LAB float64 массив (H,W,3)"""
    arr = np.array(rgb_array, dtype=np.float64) / 255.0
    mask = arr > 0.04045
    arr[mask] = ((arr[mask] + 0.055) / 1.055) ** 2.4
    arr[~mask] = arr[~mask] / 12.92
    X = arr[..., 0]*0.4124564 + arr[..., 1]*0.3575761 + arr[..., 2]*0.1804375
    Y = arr[..., 0]*0.2126729 + arr[..., 1]*0.7151522 + arr[..., 2]*0.0721750
    Z = arr[..., 0]*0.0193339 + arr[..., 1]*0.1191920 + arr[..., 2]*0.9503041
    X /= 0.95047
    Z /= 1.08883
    def f(t):
        res = np.empty_like(t)
        m = t > 0.008856
        res[m] = t[m] ** (1.0/3.0)
        res[~m] = 7.787*t[~m] + 16.0/116.0
        return res
    fx, fy, fz = f(X), f(Y), f(Z)
    return np.stack([116.0*fy - 16.0, 500.0*(fx-fy), 200.0*(fy-fz)], axis=-1)

def lab2rgb(lab_matrix):
    """LAB float64 массив → RGB float64 [0..1]"""
    lab = np.array(lab_matrix, dtype=np.float64)
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    fy = (L + 16.0)/116.0
    fx = a/500.0 + fy
    fz = fy - b/200.0
    def f_inv(t):
        res = np.empty_like(t)
        m = t > 0.206897
        res[m] = t[m]**3
        res[~m] = (t[~m] - 16.0/116.0)/7.787
        return res
    X = f_inv(fx)*0.95047
    Y = f_inv(fy)*1.00000
    Z = f_inv(fz)*1.08883
    r = X*3.2404542 + Y*-1.5371385 + Z*-0.4985314
    g = X*-0.9692660 + Y*1.8760108  + Z*0.0415560
    b = X*0.0556434  + Y*-0.2040259 + Z*1.0572252
    rgb = np.clip(np.stack([r, g, b], axis=-1), 0, 1)
    m = rgb > 0.0031308
    rgb[m] = 1.055*(rgb[m]**(1.0/2.4)) - 0.055
    rgb[~m] = rgb[~m]*12.92
    return rgb

def lab_to_rgb_tuple(lab_color):
    rgb_float = lab2rgb([[lab_color]])[0][0]
    return tuple(int(c*255) for c in rgb_float)

def rgb_tuple_to_lab(rgb_color):
    """Одиночный RGB кортеж (0-255) → LAB кортеж"""
    arr = np.array([[[rgb_color[0], rgb_color[1], rgb_color[2]]]], dtype=np.uint8)
    return tuple(rgb2lab(arr)[0][0])


# ─────────────────────────────────────────────
#  Загрузка изображения
# ─────────────────────────────────────────────

def open_file(karta_input):
    global pixels, x, y, blur_radius, sample_processed, img_lab_global
    sample = Image.open(karta_input).convert('RGB')
    if blur_radius > 0:
        sample = sample.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    pixels = sample.load()
    x, y = sample.size
    sample_processed = sample
    # Строим LAB-матрицу один раз — все алгоритмы работают с ней
    img_np = np.array(sample_processed)
    img_lab_global = rgb2lab(img_np)   # shape: (H, W, 3), индексация [row=py, col=px]
    print(f"Изображение загружено: {x}x{y} пикселей")


# ─────────────────────────────────────────────
#  DELTA-E СЕГМЕНТАЦИЯ (основной метод)
# ─────────────────────────────────────────────

def segment_by_color(target_rgb, tolerance=None, layer_name=None):
    """
    Создаёт слой по эталонному цвету из легенды.

    target_rgb  — кортеж (R, G, B) 0-255, взятый пипеткой с карты/легенды
    tolerance   — порог Delta-E (по умолчанию delta_e_tolerance из настроек)
    layer_name  — имя слоя; если None, генерируется автоматически

    Возвращает: (success: bool, message: str)
    """
    global sp_sloy, img_lab_global, delta_e_tolerance

    if img_lab_global is None:
        return False, "Сначала загрузите карту!"

    tol = tolerance if tolerance is not None else delta_e_tolerance
    target_lab = np.array(rgb_tuple_to_lab(target_rgb))  # (3,)

    status_message_set("Сегментация Delta-E...")

    # Векторизованный расчёт Delta-E для всего изображения за один проход
    # img_lab_global имеет форму (H, W, 3) — H=y, W=x
    diff = img_lab_global - target_lab          # (H, W, 3)
    delta_e = np.sqrt(np.sum(diff**2, axis=-1)) # (H, W)
    mask = delta_e < tol                         # (H, W) булева маска

    # Убираем одиночные пиксели (шум, артефакты JPEG)
    mask = _morphology_clean(mask)

    # Собираем координаты пикселей
    # np.argwhere возвращает [row, col] = [py, px]
    coords = np.argwhere(mask)
    sp_pix = set((int(c[1]), int(c[0])) for c in coords)  # (px, py)

    if len(sp_pix) == 0:
        return False, f"Цвет не найден на карте (tolerance={tol}). Попробуйте увеличить порог."

    name = layer_name if layer_name else f"{len(sp_sloy)+1}"
    lab_color = tuple(target_lab.tolist())

    # Проверяем дубликат имени
    for layer in sp_sloy:
        if str(layer.name) == str(name):
            name = f"{name}_{len(sp_sloy)}"

    noviy_sloy = clas.Sloy(
        name=name,
        rgb=target_rgb,
        lab=lab_color,
        sp_pix=sp_pix,
        vozrast="???",
        description=""
    )
    sp_sloy.append(noviy_sloy)
    status_message_set("Система готова")
    return True, f"Слой '{name}': {len(sp_pix)} пикселей (Delta-E ≤ {tol})"


# ─────────────────────────────────────────────
#  WATERSHED АВТОСЕГМЕНТАЦИЯ
# ─────────────────────────────────────────────

def segment_watershed(min_dist=None):
    """
    Полностью автоматическая сегментация Watershed.
    Не требует указания цвета — находит все однородные зоны сам.
    Используется как альтернатива старому K-Means.

    min_dist — минимальное расстояние между центрами зон (пикселей)
    """
    global sp_sloy, img_lab_global, status_message, watershed_min_dist

    if img_lab_global is None:
        return False, "Сначала загрузите карту!"

    md = min_dist if min_dist is not None else watershed_min_dist

    status_message_set("Watershed: вычисление градиента...")

    # 1. Градиент по L-каналу (яркость) — именно здесь проходят границы слоёв
    L = img_lab_global[..., 0]  # (H, W)
    grad = _sobel_gradient(L)   # (H, W) — перепады яркости

    status_message_set("Watershed: поиск локальных центров зон...")

    # 2. Локальные минимумы градиента = центры однородных зон
    markers = _find_local_minima(grad, min_distance=md)  # (H, W) int

    n_markers = markers.max()
    if n_markers == 0:
        return False, "Watershed не нашёл зон. Попробуйте уменьшить min_dist."

    status_message_set(f"Watershed: заполнение {n_markers} зон...")

    # 3. Собственно Watershed — заполняет зоны от маркеров до границ
    labels = _watershed_fill(grad, markers)  # (H, W) int, 0=граница

    # 4. Каждая метка → слой (теперь со слиянием похожих цветов!)
    sp_sloy.clear()
    unique_labels = np.unique(labels)
    unique_labels = unique_labels[unique_labels > 0]  # убираем фон/границы

    status_message_set(f"Watershed: группировка и слияние слоёв...")

    for label_id in unique_labels:
        mask = labels == label_id
        coords = np.argwhere(mask)
        
        # 1. Отсекаем микро-шум (можно поставить от 100 до 500 пикселей)
        if len(coords) < 100:  
            continue

        # 2. Вычисляем средний LAB-цвет текущей зоны
        lab_vals = img_lab_global[mask]           # (N, 3)
        mean_lab = lab_vals.mean(axis=0)          # [L, a, b]

        # 3. Пытаемся найти уже созданный слой с очень похожим цветом
        merged = False
        for layer in sp_sloy:
            l0, a0, b0 = layer.lab                # Цвет уже существующего слоя
            l1, a1, b1 = mean_lab[0], mean_lab[1], mean_lab[2]
            
            # Считаем расстояние Delta-E между средними цветами
            delta_e = np.sqrt((l0 - l1)**2 + (a0 - a1)**2 + (b0 - b1)**2)
            
            # МЕХАНИЗМ СЛИЯНИЯ: Если цвета почти идентичны (Delta-E < 8.0), 
            # объединяем эту зону с уже существующим слоем
            if delta_e < 7.0:  
                sp_pix = set((int(c[1]), int(c[0])) for c in coords)
                layer.sp_pix.update(sp_pix)       # Добавляем пиксели в существующий set
                merged = True
                break                             # Выходим из внутреннего цикла, зона успешно слита

        # 4. Если похожего цвета не нашлось, регистрируем новый уникальный слой
        if not merged:
            sp_pix = set((int(c[1]), int(c[0])) for c in coords)
            mean_lab_tuple = (float(mean_lab[0]), float(mean_lab[1]), float(mean_lab[2]))
            mean_rgb = lab_to_rgb_tuple(mean_lab_tuple) #

            noviy_sloy = clas.Sloy(
                name=f"Слой_{len(sp_sloy) + 1}",      # Даем читаемое имя вместо ID метки
                rgb=mean_rgb,
                lab=mean_lab_tuple,
                sp_pix=sp_pix,
                vozrast="???",
                description=""
            )
            sp_sloy.append(noviy_sloy)

    status_message_set("Система готова")
    return True, f"Watershed: сформировано {len(sp_sloy)} уникальных цветовых слоёв"


# ─────────────────────────────────────────────
#  Вспомогательные алгоритмы (без skimage)
# ─────────────────────────────────────────────

def _sobel_gradient(L):
    """Sobel-фильтр для поиска границ. L — 2D массив яркости."""
    Kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=np.float64)
    Ky = np.array([[-1, -2, -1], [0, 0, 0], [1, 2, 1]], dtype=np.float64)
    from numpy.lib.stride_tricks import sliding_window_view
    # Pad и свёртка через sliding window (без scipy)
    padded = np.pad(L, 1, mode='edge')
    windows = sliding_window_view(padded, (3, 3))  # (H, W, 3, 3)
    Gx = np.einsum('hwij,ij->hw', windows, Kx)
    Gy = np.einsum('hwij,ij->hw', windows, Ky)
    return np.sqrt(Gx**2 + Gy**2)

def _find_local_minima(grad, min_distance=15):
    """
    Ищет локальные минимумы градиента — центры однородных зон.
    Возвращает матрицу маркеров (0=нет маркера, >0=номер маркера).
    """
    H, W = grad.shape
    step = min_distance
    markers = np.zeros((H, W), dtype=np.int32)
    marker_id = 1

    # Делим изображение на блоки, в каждом берём минимум градиента
    for row in range(0, H - step, step):
        for col in range(0, W - step, step):
            block = grad[row:row+step, col:col+step]
            local_min_idx = np.unravel_index(np.argmin(block), block.shape)
            abs_r = row + local_min_idx[0]
            abs_c = col + local_min_idx[1]
            if markers[abs_r, abs_c] == 0:
                markers[abs_r, abs_c] = marker_id
                marker_id += 1

    return markers

def _watershed_fill(grad, markers):
    """
    Упрощённый Watershed через BFS от маркеров.
    grad    — (H, W) карта градиента
    markers — (H, W) int, >0 там где маркер
    Возвращает (H, W) int с метками зон.
    """
    import heapq
    H, W = grad.shape
    labels = markers.copy()
    visited = markers > 0

    # Приоритетная очередь: (значение градиента, row, col, метка)
    heap = []
    for r in range(H):
        for c in range(W):
            if markers[r, c] > 0:
                heapq.heappush(heap, (float(grad[r, c]), r, c, markers[r, c]))

    neighbors = [(-1,0),(1,0),(0,-1),(0,1)]

    while heap:
        val, r, c, lbl = heapq.heappop(heap)
        for dr, dc in neighbors:
            nr, nc = r+dr, c+dc
            if 0 <= nr < H and 0 <= nc < W and not visited[nr, nc]:
                visited[nr, nc] = True
                labels[nr, nc] = lbl
                heapq.heappush(heap, (float(grad[nr, nc]), nr, nc, lbl))

    return labels

def _morphology_clean(mask, min_neighbors=2):
    """
    Убирает изолированные пиксели (шум).
    Быстрая свёртка вместо цикла по set.
    """
    kernel = np.ones((3, 3), dtype=np.uint8)
    kernel[1, 1] = 0  # не считаем сам пиксель
    from numpy.lib.stride_tricks import sliding_window_view
    padded = np.pad(mask.astype(np.uint8), 1, mode='constant')
    windows = sliding_window_view(padded, (3, 3))
    neighbor_count = np.einsum('hwij,ij->hw', windows, kernel)
    return mask & (neighbor_count >= min_neighbors)


# ─────────────────────────────────────────────
#  Управление слоями
# ─────────────────────────────────────────────

def status_message_set(msg):
    global status_message
    status_message = msg

def add_new_empty_layer(name, rgb_color):
    global sp_sloy, min_d2
    for layer in sp_sloy:
        if str(layer.name) == str(name):
            return False, "Слой с таким именем уже существует!"
    lab_color = rgb_tuple_to_lab(rgb_color)
    raspred.create_custom_layer(name, rgb_color, lab_color, sp_sloy, min_d2)
    return True, "Слой успешно создан"

def filter_isolated_pixels():
    """Постобработка: убирает одиночные пиксели из всех слоёв через быструю свёртку."""
    global sp_sloy
    print("Фильтрация изолированных пикселей...")
    for s in sp_sloy:
        if not s.sp_pix:
            continue
        coords = np.array(list(s.sp_pix))  # (N, 2) — px, py
        if len(coords) < 3:
            continue
        # Строим бинарную маску
        max_px = coords[:, 0].max() + 2
        max_py = coords[:, 1].max() + 2
        mask = np.zeros((max_py, max_px), dtype=np.uint8)
        for px, py in coords:
            mask[py, px] = 1
        clean = _morphology_clean(mask.astype(bool))
        new_pix = set()
        for py_i, px_i in np.argwhere(clean):
            new_pix.add((int(px_i), int(py_i)))
        s.sp_pix = new_pix


# ─────────────────────────────────────────────
#  ГЛАВНЫЙ ПАЙПЛАЙН (старый main_all — Watershed)
# ─────────────────────────────────────────────

def main_all(karta_input):
    """
    Полностью автоматический анализ — теперь через Watershed вместо K-Means.
    Вызывается кнопкой 'Новый расчёт'.
    Для точной работы рекомендуется использовать сегментацию по легенде
    через инструмент 'Пипетка' после загрузки карты.
    """
    global status_message, sp_sloy, karta

    status_message = "Шаг 1/3: Загрузка и подготовка карты..."
    karta = karta_input
    open_file(karta_input)

    status_message = "Шаг 2/3: Автосегментация Watershed..."
    success, msg = segment_watershed()
    if not success:
        status_message = f"Ошибка: {msg}"
        return

    status_message = "Шаг 3/3: Очистка геометрии..."
    filter_isolated_pixels()

    status_message = f"Готово. {msg}"


# ─────────────────────────────────────────────
#  ЭКСПОРТ GeoJSON (для ArcGIS / QGIS)
# ─────────────────────────────────────────────

def export_geojson(output_path):
    """
    Экспортирует все слои в GeoJSON.
    Требует выполненной геопривязки (geo_system.is_calibrated).
    Файл открывается напрямую в ArcGIS, QGIS, MapInfo.
    """
    if not geo_system.is_calibrated:
        return False, "Сначала выполните геопривязку карты!"

    features = []
    for layer in sp_sloy:
        if not layer.sp_pix:
            continue

        # Строим маску и извлекаем контуры через Marching Squares (упрощённый)
        coords = np.array(list(layer.sp_pix))
        if len(coords) < 4:
            continue

        max_px = int(coords[:, 0].max()) + 2
        max_py = int(coords[:, 1].max()) + 2
        mask = np.zeros((max_py, max_px), dtype=np.uint8)
        for px, py in layer.sp_pix:
            if py < max_py and px < max_px:
                mask[py, px] = 1

        # Трассируем внешний контур через обход границы
        contour_pixels = _trace_boundary(mask)

        if not contour_pixels:
            # Если контур не найден — берём выпуклую оболочку из пикселей
            contour_pixels = _convex_hull_pixels(list(layer.sp_pix)[:200])

        # Конвертируем пиксели контура в геокоординаты
        geo_ring = []
        for px, py in contour_pixels:
            geo_pt = geo_system.pixel_to_geo(px, py)
            if geo_pt:
                lat, lon = geo_pt
                geo_ring.append([round(lon, 7), round(lat, 7)])  # GeoJSON: [lon, lat]

        if len(geo_ring) < 3:
            continue

        # Замыкаем кольцо
        if geo_ring[0] != geo_ring[-1]:
            geo_ring.append(geo_ring[0])

        feature = {
            "type": "Feature",
            "properties": {
                "name": str(layer.name),
                "vozrast": layer.vozrast,
                "description": layer.description,
                "color_rgb": list(layer.rgb),
                "pixel_count": len(layer.sp_pix)
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [geo_ring]
            }
        }
        features.append(feature)

    geojson = {
        "type": "FeatureCollection",
        "crs": {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}
        },
        "features": features
    }

    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(geojson, f, ensure_ascii=False, indent=2)
        return True, f"Экспортировано {len(features)} слоёв → {output_path}"
    except Exception as e:
        return False, f"Ошибка записи: {e}"


def _trace_boundary(mask):
    """
    Обходит внешнюю границу бинарной маски по алгоритму Moore Neighborhood.
    Возвращает список (px, py) — контур полигона.
    """
    rows, cols = np.where(mask > 0)
    if len(rows) == 0:
        return []

    # Стартовая точка — самый верхний-левый пиксель
    start_r = int(rows.min())
    start_c = int(cols[rows == rows.min()].min())

    contour = []
    visited_edges = set()
    r, c = start_r, start_c
    # Направления: право, право-вниз, вниз, лево-вниз, лево, лево-вверх, вверх, право-вверх
    dirs = [(0,1),(1,1),(1,0),(1,-1),(0,-1),(-1,-1),(-1,0),(-1,1)]
    direction = 0
    H, W = mask.shape
    max_steps = len(rows) * 2 + 10

    for _ in range(max_steps):
        contour.append((c, r))  # (px, py)
        edge_key = (r, c, direction)
        if edge_key in visited_edges and len(contour) > 4:
            break
        visited_edges.add(edge_key)

        # Ищем следующий граничный пиксель по часовой стрелке
        found = False
        for i in range(8):
            d = (direction + 6 + i) % 8  # начинаем с направления "назад-лево"
            dr, dc = dirs[d]
            nr, nc = r+dr, c+dc
            if 0 <= nr < H and 0 <= nc < W and mask[nr, nc]:
                r, c = nr, nc
                direction = d
                found = True
                break
        if not found:
            break

    return contour

def _convex_hull_pixels(points):
    """Упрощённая выпуклая оболочка (подарочная упаковка) для небольшого набора точек."""
    if len(points) < 3:
        return points
    pts = [(float(p[0]), float(p[1])) for p in points]

    def cross(O, A, B):
        return (A[0]-O[0])*(B[1]-O[1]) - (A[1]-O[1])*(B[0]-O[0])

    pts = sorted(set(pts))
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    return [(int(p[0]), int(p[1])) for p in hull]


# ─────────────────────────────────────────────
#  Сохранение / загрузка проекта
# ─────────────────────────────────────────────

def save_project(project_name="project_save.json"):
    global karta, sp_sloy, control_points
    project_data = {
        "karta": karta,
        "control_points": control_points,
        "layers": [layer.to_dict() for layer in sp_sloy]
    }
    with open(project_name, "w", encoding="utf-8") as f:
        json.dump(project_data, f, ensure_ascii=False, indent=4)
    print("Проект успешно сохранен!")
    return True

def load_project(project_name="project_save.json"):
    global karta, sp_sloy, control_points
    if not os.path.exists(project_name):
        print(f"Файл проекта {project_name} не найден!")
        return False
    print(f"Загрузка проекта {project_name}...")
    with open(project_name, "r", encoding="utf-8") as f:
        project_data = json.load(f)
    karta = project_data["karta"]
    open_file(karta)
    control_points = project_data.get("control_points", [])
    if len(control_points) >= 3:
        geo_system.calibrate(control_points)
    sp_sloy.clear()
    for layer_dict in project_data["layers"]:
        sp_sloy.append(clas.Sloy.from_dict(layer_dict))
    print(f"Успешно загружено слоёв: {len(sp_sloy)}")
    return True


# ─────────────────────────────────────────────
#  Высотные данные SRTM
# ─────────────────────────────────────────────

def compute_all_elevations(hgt_folder="dem_data"):
    global elevation_matrix, x, y, geo_system
    if not geo_system or not geo_system.is_calibrated:
        print("Ошибка: Система координат не откалибрована!")
        return False
    print("Запуск глобального расчёта рельефа...")
    A, B, C = geo_system.trans_matrix['lat']
    D, E, F = geo_system.trans_matrix['lon']
    px_indices = np.arange(x)
    py_indices = np.arange(y)
    PX, PY = np.meshgrid(px_indices, py_indices, indexing='ij')
    LATS = A*PX + B*PY + C
    LONS = D*PX + E*PY + F
    elevation_matrix = np.zeros((x, y), dtype=np.int16)
    lat_floors = np.floor(LATS).astype(int)
    lon_floors = np.floor(LONS).astype(int)
    unique_tiles = set(zip(lat_floors.ravel(), lon_floors.ravel()))
    for lat_floor, lon_floor in unique_tiles:
        lat_char = 'N' if lat_floor >= 0 else 'S'
        lon_char = 'E' if lon_floor >= 0 else 'W'
        filename = f"{lat_char}{abs(lat_floor):02d}{lon_char}{abs(lon_floor):03d}.hgt"
        filepath = os.path.join(hgt_folder, filename)
        tile_mask = (lat_floors == lat_floor) & (lon_floors == lon_floor)
        if not np.any(tile_mask):
            continue
        if os.path.exists(filepath):
            hgt_grid = np.fromfile(filepath, dtype='>i2').reshape((1201, 1201))
            d_lats = LATS[tile_mask] - lat_floor
            d_lons = LONS[tile_mask] - lon_floor
            rows = np.clip(((1.0 - d_lats)*1200).astype(int), 0, 1200)
            cols = np.clip((d_lons*1200).astype(int), 0, 1200)
            heights = hgt_grid[rows, cols]
            heights[heights == -32768] = 0
            elevation_matrix[tile_mask] = heights
            print(f"Плитка {filename} наложена.")
        else:
            elevation_matrix[tile_mask] = 0
    print("Матрица высот построена!")
    return True

if __name__ == "__main__":
    from interface import MyApp
    MyApp().run()
