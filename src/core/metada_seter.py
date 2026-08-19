from pathlib import Path
from PIL import Image
import piexif
import requests
import tempfile
from src.config.config_cloudinary import ConfigCloudinary


def decimal_to_dms(decimal):
    """Convert decimal degrees to EXIF DMS format."""
    decimal = abs(float(decimal))
    degrees = int(decimal)
    minutes_float = (decimal - degrees) * 60
    minutes = int(minutes_float)
    seconds = round((minutes_float - minutes) * 60 * 100)
    return (
        (degrees, 1),
        (minutes, 1),
        (seconds, 100),
    )


def update_image_metadata(
    image_path: str,  # Cloudinary URL or local path to the image
    output_data: dict,
    cloudinary: ConfigCloudinary,
) -> str:
    """
    Download image from Cloudinary (or load locally), inject SEO metadata,
    re-upload to Cloudinary, and delete the local temp file.

    Parameters
    ----------
    image_path : str
        Cloudinary URL (https://...) or a local file path.
    output_data : dict
        Metadata dict with keys: file_name, title, description,
        caption, SEO_keywords, assign_location.
    cloudinary : ConfigCloudinary
        Cloudinary config instance used for uploading.

    Returns
    -------
    str
        The Cloudinary URL of the uploaded image.
    """

    seo_filename = output_data["file_name"].strip().lower()

    # ------------------------------------------------------------------
    # 1. Fetch the image — from URL or local path
    # ------------------------------------------------------------------
    is_url = str(image_path).startswith("http://") or str(image_path).startswith("https://")

    if is_url:
        response = requests.get(image_path, timeout=30)
        response.raise_for_status()

        # Detect extension from URL (fallback to .jpg)
        url_path = image_path.split("?")[0]  # strip query params
        extension = Path(url_path).suffix.lower() or ".jpg"

        # Write to a temp file so PIL can open it
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=extension)
        tmp.write(response.content)
        tmp.close()
        local_source_path = Path(tmp.name)
    else:
        local_source_path = Path(image_path)
        extension = local_source_path.suffix.lower()

    # ------------------------------------------------------------------
    # 2. Open image and load/init EXIF
    # ------------------------------------------------------------------
    img = Image.open(local_source_path)

    try:
        exif_dict = piexif.load(img.info.get("exif", b""))
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

    # ------------------------------------------------------------------
    # 3. Title → XPTitle + DocumentName
    # ------------------------------------------------------------------
    title = output_data.get("title", "")
    if title:
        exif_dict["0th"][piexif.ImageIFD.XPTitle] = title.encode("utf-16le")
        exif_dict["0th"][piexif.ImageIFD.DocumentName] = title.encode("utf-8")

    # ------------------------------------------------------------------
    # 4. Description → ImageDescription (primary tag Google reads)
    # ------------------------------------------------------------------
    description = output_data.get("description", "")
    if description:
        exif_dict["0th"][piexif.ImageIFD.ImageDescription] = description.encode("utf-8")

    # ------------------------------------------------------------------
    # 5. Caption → XPComment
    # ------------------------------------------------------------------
    caption = output_data.get("caption", "")
    if caption:
        exif_dict["0th"][piexif.ImageIFD.XPComment] = caption.encode("utf-16le")

    # ------------------------------------------------------------------
    # 6. Keywords → XPKeywords (comma-separated)
    # ------------------------------------------------------------------
    keywords_list = output_data.get("SEO_keywords", [])
    if keywords_list:
        keywords_str = ", ".join(keywords_list)
        exif_dict["0th"][piexif.ImageIFD.XPKeywords] = keywords_str.encode("utf-16le")

    # ------------------------------------------------------------------
    # 7. GPS
    # ------------------------------------------------------------------
    gps = output_data.get("assign_location", {})
    if gps:
        lat = gps["GPSLatitude"]
        lon = gps["GPSLongitude"]
        lat_ref = gps.get("GPSLatitudeRef", "N").upper()
        lon_ref = gps.get("GPSLongitudeRef", "E").upper()

        exif_dict["GPS"] = {
            piexif.GPSIFD.GPSLatitudeRef: lat_ref.encode(),
            piexif.GPSIFD.GPSLatitude: decimal_to_dms(lat),
            piexif.GPSIFD.GPSLongitudeRef: lon_ref.encode(),
            piexif.GPSIFD.GPSLongitude: decimal_to_dms(lon),
        }

    # ------------------------------------------------------------------
    # 8. Save to a second temp file with injected EXIF
    # ------------------------------------------------------------------
    exif_bytes = piexif.dump(exif_dict)

    output_tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=extension, prefix=f"{seo_filename}_"
    )
    output_tmp.close()
    output_tmp_path = Path(output_tmp.name)

    img.save(str(output_tmp_path), exif=exif_bytes, quality=95)

    # ------------------------------------------------------------------
    # 9. Upload to Cloudinary
    # ------------------------------------------------------------------
    save_path = cloudinary.upload_data_to_cloudinary(
        file_path=str(output_tmp_path),
        public_id=seo_filename,
    )

    # ------------------------------------------------------------------
    # 10. Clean up both temp files
    # ------------------------------------------------------------------
    if is_url and local_source_path.exists():
        local_source_path.unlink()

    if output_tmp_path.exists():
        output_tmp_path.unlink()

    return save_path