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


def plot_image_from_hdf5(h5_path: Path, img_temp_dir: Path) -> None:
    """将单个 HDF5 文件中的所有图像方向绘制并保存到 img_temp_dir。"""
    with h5py.File(h5_path, "r") as f:
        images_grp = f["images"]
        for direction in images_grp:
            data = images_grp[direction]["data"][:]
            data = image_destripe(data)

            fig, ax = plt.subplots()
            ax.imshow(data, cmap="Blues_r", origin="lower")
            ax.set_title(f"{h5_path.stem} - {direction}")
            ax.axis("off")

            # 生成安全文件名
            safe_direction = direction.replace("/", "_")
            out_name = f"{h5_path.stem}_{safe_direction}.png"
            fig.savefig(img_temp_dir / out_name, dpi=150, bbox_inches="tight")
            plt.close(fig)


def plot_all_images(output_dir: Path) -> None:
    """扫描 output_dir 中所有 .h5 文件，将所有图像绘制到 img_temp 子目录。"""
    img_temp_dir = output_dir / "img_temp"
    img_temp_dir.mkdir(parents=True, exist_ok=True)

    h5_files = sorted(output_dir.glob("*.h5"))
    if not h5_files:
        logger.warning("No HDF5 files found in %s", output_dir)
        return

    for h5_path in h5_files:
        plot_image_from_hdf5(h5_path, img_temp_dir)

    logger.info("Saved %d image plots to %s", len(h5_files), img_temp_dir)


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
    # ==================== 第 4 步：绘图 ====================

    logger.info("All operations completed.")


if __name__ == "__main__":
    main()
