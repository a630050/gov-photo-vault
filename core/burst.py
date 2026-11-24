from datetime import timedelta
import uuid
from PIL import Image, ImageOps, UnidentifiedImageError
from .models import Photo

BURST_WINDOW_SECONDS = 5
BURST_DHASH_THRESHOLD = 14


def compute_dhash_from_image(im):
    gray = ImageOps.exif_transpose(im).convert('L')
    pixels = list(gray.resize((9, 8), Image.Resampling.LANCZOS).getdata())
    bits = (pixels[y * 9 + x] > pixels[y * 9 + x + 1] for y in range(8) for x in range(8))
    return format(sum(int(v) << i for i, v in enumerate(bits)), '016x')


def ensure_photo_dhash(photo):
    if photo.dhash:
        return photo.dhash
    for field in (photo.preview, photo.file):
        if not field:
            continue
        try:
            with field.open('rb') as src, Image.open(src) as im:
                photo.dhash = compute_dhash_from_image(im)
                photo.save(update_fields=['dhash'])
                return photo.dhash
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
            continue
    return ''


def dhash_distance(a, b):
    if not a or not b or len(a) != 16 or len(b) != 16:
        return 64
    return (int(a, 16) ^ int(b, 16)).bit_count()


def assign_burst_group(photo):
    """Assign or merge burst_group for a submitted photo within +-5s and dHash <= 14."""
    if photo.status != 'submitted' or not photo.captured:
        return ''
    my_hash = ensure_photo_dhash(photo)
    if not my_hash:
        return ''

    window = timedelta(seconds=BURST_WINDOW_SECONDS)
    neighbors = list(
        Photo.objects.select_for_update()
        .filter(
            dataset_id=photo.dataset_id,
            status='submitted',
            captured__gte=photo.captured - window,
            captured__lte=photo.captured + window,
        )
        .exclude(id=photo.id)
        .order_by('captured', 'id')
    )

    matched = []
    for n in neighbors:
        nh = ensure_photo_dhash(n)
        if nh and dhash_distance(my_hash, nh) <= BURST_DHASH_THRESHOLD:
            matched.append(n)

    if not matched:
        return photo.burst_group

    existing_groups = [n.burst_group for n in matched if n.burst_group]
    if photo.burst_group and photo.burst_group not in existing_groups:
        existing_groups.append(photo.burst_group)

    group_id = existing_groups[0] if existing_groups else str(uuid.uuid4())

    if len(existing_groups) > 1:
        Photo.objects.filter(dataset_id=photo.dataset_id, burst_group__in=existing_groups[1:]).update(
            burst_group=group_id,
            burst_cover=False,
        )

    to_update = [n.id for n in matched if n.burst_group != group_id]
    if to_update:
        Photo.objects.filter(id__in=to_update).update(burst_group=group_id)

    if photo.burst_group != group_id:
        photo.burst_group = group_id
        photo.save(update_fields=['burst_group'])

    return group_id
