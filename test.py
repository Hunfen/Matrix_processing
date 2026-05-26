#!/Users/hunfen/Documents/GitHub/STM_DataProcessing/.venv/bin/python
import csv
import logging
import re
import sys
from pathlib import Path

import access2thematrix
import h5py
import matplotlib.pyplot as plt
import numpy as np

logger = logging.getLogger(__name__)


def natural_key(s: str) -> list:
    """Split the string by numeric parts and convert them to integers for natural sorting."""
    return [
        int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", s)
    ]


def scan_and_classify_files(input_folder: Path, output_dir: Path) -> dict:
    """Scan input folder, classify files and write CSV lists to output_dir.

    Returns:
        dict with keys: 'filenames', 'metadata_files', 'spectrum_files', 'image_files'
    """
    # Collect all file names (excluding .csv), naturally sorted
    filenames = sorted(
        (
            item.name
            for item in input_folder.iterdir()
            if item.is_file() and item.suffix.lower() != ".csv"
        ),
        key=natural_key,
    )

    # Write file_list.csv
    csv_path = output_dir / "file_list.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["File Name"])
        for name in filenames:
            writer.writerow([name])
    logger.info("Saved %d file names to %s", len(filenames), csv_path)

    # Classify by extension
    metadata_files = []
    spectrum_files = []
    image_files = []

    for name in filenames:
        suffix = Path(name).suffix.lower()
        if not suffix:
            continue
        if suffix == ".mtrx":
            metadata_files.append(name)
        elif re.search(r"\(v\)", suffix):
            spectrum_files.append(name)
        else:
            image_files.append(name)

    # Helper to write classification CSV
    def _write_csv(filepath: Path, data: list) -> None:
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["File Name"])
            for name in data:
                writer.writerow([name])

    _write_csv(output_dir / "metadata_files.csv", metadata_files)
    _write_csv(output_dir / "spectrum_files.csv", spectrum_files)
    _write_csv(output_dir / "image_files.csv", image_files)

    logger.info(
        "Classification complete: metadata %d, spectrum %d, image %d",
        len(metadata_files),
        len(spectrum_files),
        len(image_files),
    )

    return {
        "filenames": filenames,
        "metadata_files": metadata_files,
        "spectrum_files": spectrum_files,
        "image_files": image_files,
    }


def convert_image_file(image_path: Path, output_dir: Path) -> None:
    """Convert an image file (readable by access2thematrix) to HDF5.

    The output file is named <original name>.h5 and placed in output_dir.
    HDF5 internal structure:
        /images/<direction>/
            data : 2D image array
            .attrs: angle, channel_name_and_unit, height, width, x_offset, y_offset
        /parameters/<parameter_name>/
            .attrs: value (numeric), unit (string)
    """
    m = access2thematrix.MtrxData()
    traces, msg = m.open(str(image_path))
    if msg:
        logger.warning("%s: %s", image_path.name, msg)

    # Get full experiment parameters: eepas : [[name, value, unit], ...]
    eepas, _ = m.get_experiment_element_parameters()

    # Iterate over all directions, extract image data (im is an object with attributes)
    image_data = {}
    for direction in traces.values():
        im, err = m.select_image(direction)
        if im is not None:
            image_data[direction] = im
        else:
            logger.warning(
                "  [%s] No data for direction %s: %s", image_path.name, direction, err
            )

    if not image_data:
        logger.warning("Skipping %s: no image data available", image_path.name)
        return

    h5_path = output_dir / (image_path.name + ".h5")
    with h5py.File(h5_path, "w") as f:
        # 1. Image data per direction + simplified parameters
        images_grp = f.create_group("images")
        for direction, im in image_data.items():
            safe_dir = direction.replace("/", "_")
            grp = images_grp.create_group(safe_dir)
            # Image array
            grp.create_dataset("data", data=im.data, compression="gzip")
            # Simplified parameters (attributes of the im object)
            for key in [
                "angle",
                "channel_name_and_unit",
                "height",
                "width",
                "x_offset",
                "y_offset",
            ]:
                grp.attrs[key] = getattr(im, key)

        # 2. Full parameters: one subgroup per parameter, storing value and unit
        param_grp = f.create_group("parameters")
        for name, value, unit in eepas:
            safe_name = name.replace("/", "_")
            sub_grp = param_grp.create_group(safe_name)
            sub_grp.attrs["value"] = value
            sub_grp.attrs["unit"] = unit

    logger.info("Generated: %s", h5_path)


def image_destripe(data: np.ndarray) -> np.ndarray:
    out = data.astype(np.float64, copy=True)
    for col in range(out.shape[1] - 2, -1, -1):
        out[:, col] -= np.median(out[:, col] - out[:, col + 1])
    return out


def convert_curve_file(
    curve_path: Path, image_curve_match: dict, output_dir: Path
) -> None:
    """Convert a spectrum file (readable by access2thematrix) to HDF5.

    The output file is named <original name>.h5 and placed in output_dir.
    HDF5 internal structure:
        /curves/<direction>/
            data : 2D array
            .attrs:
                x_data_name, x_data_unit, y_data_name, y_data_unit,
                <referenced_by keys> : each a string or comma-separated string
        /parameters/<parameter_name>/
            .attrs: value (numeric), unit (string)
    """

    m = access2thematrix.MtrxData()
    traces, msg = m.open(str(curve_path))
    if msg:
        logger.warning("%s: %s", curve_path.name, msg)

    # Get full experiment parameters
    eepas, _ = m.get_experiment_element_parameters()

    # Iterate over all directions, extract curve data
    curve_data = {}
    for direction in traces.values():
        Cu, err = m.select_curve(direction)
        if Cu is not None:
            curve_data[direction] = Cu
            data_file_name = Cu.referenced_by.get("Data File Name", "")
            logger.info(
                "data_file_name: %r, curve: %s", data_file_name, curve_path.name
            )
            if data_file_name in image_curve_match:
                image_curve_match[data_file_name].append(curve_path.name)
            else:
                image_curve_match[data_file_name] = [curve_path.name]
        else:
            logger.warning(
                "  [%s] No data for direction %s: %s", curve_path.name, direction, err
            )

    if not curve_data:
        logger.warning("Skipping %s: no curve data available", curve_path.name)
        return

    h5_path = output_dir / (curve_path.name + ".h5")
    with h5py.File(h5_path, "w") as f:
        # 1. Curve data per direction + simplified metadata
        curves_grp = f.create_group("curves")
        for direction, Cu in curve_data.items():
            safe_dir = direction.replace("/", "_")
            grp = curves_grp.create_group(safe_dir)
            # Store whole data array
            grp.create_dataset("data", data=Cu.data, compression="gzip")

            # --- Simplified metadata: referenced_by dict -> individual attributes ---
            for key, val in Cu.referenced_by.items():
                # Sanitize attribute name: replace spaces/parens with underscores
                safe_key = key.replace(" ", "_").replace("(", "_").replace(")", "_")
                # If value is a list, convert to comma-separated string
                if isinstance(val, list):
                    val = ",".join(str(v) for v in val)
                grp.attrs[safe_key] = val

            # --- x_data_name_and_unit list -> two separate attributes ---
            grp.attrs["x_data_name"] = Cu.x_data_name_and_unit[0]
            grp.attrs["x_data_unit"] = Cu.x_data_name_and_unit[1]

            # --- y_data_name_and_unit list -> two separate attributes ---
            grp.attrs["y_data_name"] = Cu.y_data_name_and_unit[0]
            grp.attrs["y_data_unit"] = Cu.y_data_name_and_unit[1]

        # 2. Full parameters: one subgroup per parameter, storing value and unit
        param_grp = f.create_group("parameters")
        for name, value, unit in eepas:
            safe_name = name.replace("/", "_")
            sub_grp = param_grp.create_group(safe_name)
            sub_grp.attrs["value"] = value
            sub_grp.attrs["unit"] = unit

    logger.info("Generated: %s", h5_path)


def plot_image_from_hdf5(h5_path: Path, img_dir: Path) -> None:
    """Plot all image directions from a single HDF5 file and save them to img_dir as PNGs."""
    with h5py.File(h5_path, "r") as f:
        images_grp = f["images"]
        for direction in images_grp:
            data = images_grp[direction]["data"][:]
            data = image_destripe(data)

            fig, ax = plt.subplots(figsize=(5, 5))
            ax.imshow(data, cmap="Blues_r", origin="lower")
            ax.axis("off")

            safe_direction = direction.replace("/", "_")
            out_name = f"{h5_path.stem}_{safe_direction}.png"
            fig.savefig(img_dir / out_name, dpi=150, bbox_inches="tight")
            plt.close(fig)


def plot_image_from_array(
    data: np.ndarray,
    save_path: Path,
) -> None:
    """Plot and save a single 2D array after destriping.

    Args:
        data: 2D numpy array to visualize.
        save_path: Full output file path for the saved PNG.
    """
    data = image_destripe(data)
    fig, ax = plt.subplots(figsize=(5, 5))
    ax.imshow(data, cmap="Blues_r", origin="lower")
    ax.axis("off")
    fig.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def plot_all_images(output_dir: Path, image_files: list[str]) -> None:
    """Plot image data for each image file listed in image_files, saving to images subdirectory.

    Each filename in image_files is assumed to have a corresponding .h5 file in output_dir.
    """
    img_dir = output_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    count = 0

    for name in image_files:
        h5_path = output_dir / (name + ".h5")
        if h5_path.exists():
            plot_image_from_hdf5(h5_path, img_dir)
            count += 1
        else:
            logger.warning(
                "Missing HDF5 for image file %s (expected %s)", name, h5_path
            )

    logger.info("Saved %d image plots to %s", count, img_dir)


def get_image_extent(h5_path: Path) -> list:
    """从 HDF5 图像文件中读取第一个通道的几何参数，返回 imshow 的 extent。

    Parameters
    ----------
    h5_path : Path
        HDF5 图像文件路径，必须包含 /images/<direction>/ 子组。

    Returns
    -------
    extent : list
        [left, right, bottom, top]
    """
    with h5py.File(h5_path, "r") as f:
        images_grp = f["images"]
        # 取第一个 direction（按迭代器顺序，可认为是主要通道）
        first_dir = next(iter(images_grp))
        grp = images_grp[first_dir]
        height = float(grp.attrs["height"])
        width = float(grp.attrs["width"])
        x_offset = float(grp.attrs["x_offset"])
        y_offset = float(grp.attrs["y_offset"])

    left = x_offset - width / 2
    right = x_offset + width / 2
    bottom = y_offset - height / 2
    top = y_offset + height / 2
    return [left, right, bottom, top]


def plot_marker_mask(
    coords: list[list[float]],
    extent: list[float],
    save_path: Path,
    marker_size: int = 30,
) -> None:
    """生成一个透明背景的位置标记 mask 图像并保存。

    Parameters
    ----------
    coords : list of list of float
        坐标列表，每个元素为 [x, y]。
    extent : list of float
        [left, right, bottom, top] 图像的数据范围。
    save_path : Path
        输出 PNG 文件路径。
    marker_size : int
        标记点的大小。
    """
    logger.info("plot_marker_mask: extent=%s, coords=%s", extent, coords)

    colors = plt.cm.tab10([i % 10 for i in range(len(coords))])
    fig, ax = plt.subplots(figsize=(5, 5))
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.axis("off")

    for i, (x, y) in enumerate(coords):
        ax.scatter(
            x,
            y,
            color=colors[i],
            s=marker_size,
            marker="o",
            edgecolors="black",
            linewidth=0.5,
            zorder=10,
        )

    fig.savefig(save_path, dpi=150, transparent=True, bbox_inches="tight", pad_inches=0)
    plt.close(fig)


def get_curves_coords(curve_paths: list[Path]) -> list[list[float]]:
    """从 HDF5 曲线文件中读取坐标 (X_offset, Y_offset) 或 Location__m_ 属性。

    优先使用曲线组的 Location__m_ 属性（格式："x,y"），若不存在则从 parameters 中查找。
    """
    coords = []
    for path in curve_paths:
        with h5py.File(path, "r") as f:
            curves_grp = f["curves"]
            first_dir = next(iter(curves_grp))
            grp = curves_grp[first_dir]
            loc_str = grp.attrs.get("Location__m_", "")
            if loc_str:
                parts = loc_str.split(",")
                if len(parts) == 2:
                    coords.append([float(parts[0]), float(parts[1])])
    return coords


# def plot_curves_comb(
#     curve_paths: list[Path],
#     save_path: Path | None,
#     shift: bool = False,
# ) -> None:
#     """将多个 HDF5 曲线文件的第一个 trace 绘制到同一张图中。

#     颜色使用 tab10 按索引分配。轴标签取自第一个文件的元数据。
#     如果 save_path 为 None，则不保存图片。

#     Parameters
#     ----------
#     curve_paths : list of Path
#         HDF5 曲线文件路径列表。
#     save_path : Path or None
#         输出 PNG 文件路径，若为 None 则跳过保存。
#     shift : bool
#         若为 True，则将每条曲线的 y 轴依次向上偏移（零点上移），避免重叠；
#         若为 False（默认），所有曲线叠加在同一基线。
#     """
#     if not curve_paths:
#         return

#     # 如果 shift 为 True，预先遍历所有数据计算总偏移量
#     offset = 0.0
#     if shift:
#         all_y_min = float("inf")
#         all_y_max = float("-inf")
#         for cpath in curve_paths:
#             with h5py.File(cpath, "r") as f:
#                 curves_grp = f["curves"]
#                 first_dir = next(iter(curves_grp))
#                 y_data = curves_grp[first_dir]["data"][1, :]
#                 all_y_min = min(all_y_min, np.min(y_data))
#                 all_y_max = max(all_y_max, np.max(y_data))
#         offset = (all_y_max - all_y_min) * 1.2  # 相邻曲线间隔为数据范围的 1.2 倍

#     fig, ax = plt.subplots(figsize=(5, 5))
#     colors = plt.cm.tab10([i % 10 for i in range(len(curve_paths))])

#     for i, cpath in enumerate(curve_paths):
#         with h5py.File(cpath, "r") as f:
#             curves_grp = f["curves"]
#             first_dir = next(iter(curves_grp))
#             grp = curves_grp[first_dir]
#             data = grp["data"][:]
#             x_data = data[0, :]
#             y_data = data[1, :] + i * offset  # 偏移后的 y 数据
#             ax.plot(x_data, y_data, color=colors[i], linewidth=1)

#             if i == 0:
#                 x_name = grp.attrs["x_data_name"]
#                 x_unit = grp.attrs["x_data_unit"]
#                 y_name = grp.attrs["y_data_name"]
#                 y_unit = grp.attrs["y_data_unit"]
#                 ax.set_xlabel(f"{x_name} ({x_unit})")
#                 ax.set_ylabel(f"{y_name} ({y_unit})")

#     fig.tight_layout()
#     if save_path is not None:
#         fig.savefig(
#             save_path,
#             bbox_inches="tight",
#             transparent=True,
#             pad_inches=0.1,
#         )
#     plt.close(fig)


def plot_curves_comb(
    curve_paths: list[Path],
    save_path: Path | None,
    shift: bool = False,
) -> None:
    """将多个 HDF5 曲线文件的第一个 trace 绘制到同一张图中。

    颜色使用 tab10 按索引分配。轴标签取自第一个文件的元数据。
    如果 save_path 为 None，则不保存图片。

    Parameters
    ----------
    curve_paths : list of Path
        HDF5 曲线文件路径列表。
    save_path : Path or None
        输出 PNG 文件路径，若为 None 则跳过保存。
    shift : bool
        若为 True，则将每条曲线的 y 轴依次向上偏移（零点上移），避免重叠；
        若为 False（默认），所有曲线叠加在同一基线。
    """
    if not curve_paths:
        return

    # 如果 shift 为 True，预先遍历所有数据计算总偏移量
    offset = 0.0
    if shift:
        all_y_min = float("inf")
        all_y_max = float("-inf")
        for cpath in curve_paths:
            with h5py.File(cpath, "r") as f:
                curves_grp = f["curves"]
                first_dir = next(iter(curves_grp))
                y_data = curves_grp[first_dir]["data"][1, :]
                all_y_min = min(all_y_min, np.min(y_data))
                all_y_max = max(all_y_max, np.max(y_data))
        offset = (all_y_max - all_y_min) * 1.2  # 相邻曲线间隔为数据范围的 1.2 倍

    fig, ax = plt.subplots(figsize=(5, 5))
    colors = plt.cm.tab10([i % 10 for i in range(len(curve_paths))])

    # 临时文件格式: _avg_{img_name}_{m}.h5，提取最后的 m 作为谱号
    temp_pattern = re.compile(r"_(\d+)\.h5$")

    for i, cpath in enumerate(curve_paths):
        with h5py.File(cpath, "r") as f:
            curves_grp = f["curves"]
            first_dir = next(iter(curves_grp))
            grp = curves_grp[first_dir]
            data = grp["data"][:]
            x_data = data[0, :]
            y_data = data[1, :] + i * offset  # 偏移后的 y 数据

            # 提取谱号作为图例标签
            m = temp_pattern.search(cpath.name)
            label = f"m={m.group(1)}" if m else cpath.stem
            ax.plot(x_data, y_data, color=colors[i], linewidth=1, label=label)

            if i == 0:
                x_name = grp.attrs["x_data_name"]
                x_unit = grp.attrs["x_data_unit"]
                y_name = grp.attrs["y_data_name"]
                y_unit = grp.attrs["y_data_unit"]
                ax.set_xlabel(f"{x_name} ({x_unit})")
                ax.set_ylabel(f"{y_name} ({y_unit})")

    ax.legend(fontsize=8)
    fig.tight_layout()
    if save_path is not None:
        fig.savefig(
            save_path,
            bbox_inches="tight",
            transparent=True,
            pad_inches=0.1,
        )
    plt.close(fig)


def get_image_shape(h5_path: Path) -> tuple:
    """从 HDF5 图像文件中获取第一个 direction 的数据像素尺寸 (height, width)。"""
    with h5py.File(h5_path, "r") as f:
        images_grp = f["images"]
        first_dir = next(iter(images_grp))
        data = images_grp[first_dir]["data"]
        shape = data.shape  # (height, width)
    logger.info("get_image_shape: %s", shape)
    return shape


def get_curves_px_coords(curve_paths: list[Path]) -> list[list[float]]:
    """从 HDF5 曲线文件中读取像素坐标 (Location (px))，返回 [[x, y], ...] 列表。"""
    coords = []
    for path in curve_paths:
        with h5py.File(path, "r") as f:
            curves_grp = f["curves"]
            first_dir = next(iter(curves_grp))
            loc_str = curves_grp[first_dir].attrs.get("Location__px_", "")
            if loc_str:
                parts = loc_str.split(",")
                if len(parts) == 2:
                    coords.append([float(parts[0]), float(parts[1])])
    return coords


def process_curves_and_masks_px(
    output_dir: Path,
    image_curve_match: dict,
) -> None:
    """针对每个图像，将同谱号(m)的所有cycle求平均，再按每5个谱分组绘制mask与曲线组合图。

    曲线文件名格式：{prefix}--{m}_{n}.{channel}，例如
    default_2024Nov26-161654_STM-STM_Spectroscopy--3_2.Aux2(V)_mtrx
    其中 3 为谱号，2 为 cycle 号。
    """
    import re
    from collections import defaultdict

    mask_dir = output_dir / "mask"
    sts_dir = output_dir / "sts"
    mask_dir.mkdir(parents=True, exist_ok=True)
    sts_dir.mkdir(parents=True, exist_ok=True)

    for img_name, curve_names in image_curve_match.items():
        # 只保留 Aux2(V) 通道
        valid_curves = [c for c in curve_names if "Aux2(V)_mtrx" in c]
        if not valid_curves:
            continue

        img_h5 = output_dir / (img_name + ".h5")
        if not img_h5.exists():
            logger.warning("Image HDF5 not found, skipping: %s", img_h5)
            continue

        # ---------- 按谱号 m 分组 ----------
        groups: dict[int, list[str]] = defaultdict(list)
        for cname in valid_curves:
            m = re.search(r"--(\d+)_(\d+)\.", cname)
            if m is None:
                logger.warning("Cannot parse spectrum index from %s", cname)
                continue
            groups[int(m.group(1))].append(cname)

        sorted_m = sorted(groups.keys())
        if not sorted_m:
            continue

        height, width = get_image_shape(img_h5)
        extent = [0, width, 0, height]

        group_size = 5

        for idx in range(0, len(sorted_m), group_size):
            group_m = sorted_m[idx : idx + group_size]

            avg_curve_paths: list[Path] = []
            coords: list[list[float]] = []

            for m in group_m:
                curve_files = groups[m]
                y_stack = []
                x_ref = None
                attrs = {}
                direction = None

                for cname in curve_files:
                    h5_path = output_dir / (cname + ".h5")
                    if not h5_path.exists():
                        logger.warning("Missing HDF5 for curve %s", cname)
                        continue
                    with h5py.File(h5_path, "r") as f:
                        grp = f["curves"]
                        first_dir = next(iter(grp))
                        data = grp[first_dir]["data"][:]
                        if x_ref is None:
                            x_ref = data[0, :]
                            direction = first_dir
                            attrs = dict(grp[first_dir].attrs)
                        y_stack.append(data[1, :])

                if not y_stack:
                    continue
                y_mean = np.mean(y_stack, axis=0)

                # 创建临时 HDF5 文件，供 plot_curves_comb 直接使用
                avg_h5_path = output_dir / f"_avg_{img_name}_{m}.h5"
                with h5py.File(avg_h5_path, "w") as f:
                    curves_grp = f.create_group("curves")
                    grp = curves_grp.create_group(direction)
                    grp.create_dataset("data", data=np.stack([x_ref, y_mean]))
                    for key in [
                        "x_data_name",
                        "x_data_unit",
                        "y_data_name",
                        "y_data_unit",
                    ]:
                        if key in attrs:
                            grp.attrs[key] = attrs[key]

                avg_curve_paths.append(avg_h5_path)

                # 坐标取第一个 cycle 的像素坐标
                first_cname = curve_files[0]
                first_h5 = output_dir / (first_cname + ".h5")
                with h5py.File(first_h5, "r") as f:
                    grp = f["curves"]
                    first_dir2 = next(iter(grp))
                    loc_str = grp[first_dir2].attrs.get("Location__px_", "")
                if loc_str:
                    parts = loc_str.split(",")
                    if len(parts) == 2:
                        coords.append([float(parts[0]), float(parts[1])])

            if not avg_curve_paths:
                continue

            # 生成 marker mask
            mask_name = f"{img_name}_group{idx // group_size}.png"
            mask_path = mask_dir / mask_name
            plot_marker_mask(coords, extent, mask_path)

            # 生成曲线组合图
            sts_name = f"{img_name}_group{idx // group_size}.png"
            sts_path = sts_dir / sts_name
            plot_curves_comb(avg_curve_paths, sts_path)

            # 清理临时 HDF5 文件
            for p in avg_curve_paths:
                try:
                    p.unlink()
                except OSError:
                    pass


def process_curves_and_masks(
    output_dir: Path,
    image_curve_match: dict,
) -> None:
    """根据 image_curve_match 生成 marker mask 和曲线组合图。

    按照每 5 条曲线一组的方式绘制。
    """
    mask_dir = output_dir / "mask"
    sts_dir = output_dir / "sts"
    mask_dir.mkdir(parents=True, exist_ok=True)
    sts_dir.mkdir(parents=True, exist_ok=True)

    for img_name, curve_names in image_curve_match.items():
        # 筛选包含 "Aux2(V)_mtrx" 的曲线文件名
        valid_curves = [c for c in curve_names if "Aux2(V)_mtrx" in c]
        if not valid_curves:
            continue

        img_h5 = output_dir / (img_name + ".h5")
        if not img_h5.exists():
            logger.warning("Image HDF5 not found, skipping: %s", img_h5)
            continue

        extent = get_image_extent(img_h5)
        group_size = 5

        for idx in range(0, len(valid_curves), group_size):
            group = valid_curves[idx : idx + group_size]
            curve_h5_paths = [output_dir / (c + ".h5") for c in group]

            coords = get_curves_coords(curve_h5_paths)

            # 生成 marker mask
            mask_name = f"{img_name}_group{idx // group_size}.png"
            mask_path = mask_dir / mask_name
            plot_marker_mask(coords, extent, mask_path)

            # 生成曲线组合图
            sts_name = f"{img_name}_group{idx // group_size}.png"
            sts_path = sts_dir / sts_name
            plot_curves_comb(curve_h5_paths, sts_path)


def main() -> None:
    # 解析命令行参数
    if len(sys.argv) < 2:
        print("Usage: python test.py <input_folder> [output_folder]")
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    input_folder = Path(sys.argv[1])
    if not input_folder.is_dir():
        logger.error("'%s' is not a directory", input_folder)
        sys.exit(1)

    # 输出目录默认为输入目录
    output_folder = Path(sys.argv[2]) if len(sys.argv) > 2 else input_folder
    output_folder.mkdir(parents=True, exist_ok=True)

    # ==================== 第 1 步：文件扫描与分类 ====================
    classification = scan_and_classify_files(input_folder, output_folder)
    # metadata_files = classification["metadata_files"]
    spectrum_files = classification["spectrum_files"]
    image_files = classification["image_files"]
    image_curve_match = {img: [] for img in image_files}
    # ==================== 第 2 步：将图片文件转换为 HDF5 ====================
    if image_files:
        logger.info("Starting conversion of image files to HDF5...")
        for img_name in image_files:
            img_path = input_folder / img_name
            if img_path.exists():
                convert_image_file(img_path, output_folder)
            else:
                logger.warning("File not found, skipping: %s", img_path)
    else:
        logger.info("No image files to convert.")
    # ==================== 第 3 步：将谱图文件转换为 HDF5 ====================
    if spectrum_files:
        logger.info("Starting conversion of spectrum files to HDF5...")
        for spec_name in spectrum_files:
            spec_path = input_folder / spec_name
            if spec_path.exists():
                convert_curve_file(spec_path, image_curve_match, output_folder)
            else:
                logger.warning("File not found, skipping: %s", spec_path)
    else:
        logger.info("No spectrum files to convert.")

    # 输出 image_curve_match 为 CSV 以便查看对应关系
    match_csv_path = output_folder / "image_curve_match.csv"
    with open(match_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Image File", "Curve Files"])
        for img, curves in image_curve_match.items():
            writer.writerow([img, ",".join(curves)])
    logger.info("Saved image_curve_match to %s", match_csv_path)
    # ==================== 第 4 步：绘图 ====================
    plot_all_images(output_folder, image_files)
    process_curves_and_masks_px(output_folder, image_curve_match)

    logger.info("All operations completed.")


if __name__ == "__main__":
    main()
