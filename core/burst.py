from collections import Counter
from datetime import timedelta
import uuid
from PIL import Image, ImageOps, UnidentifiedImageError
from django.db import transaction
from .models import Audit, Config, Dataset, Photo

BURST_ALGO = 'dhash64-window-v2'
DEFAULT_GAP_SECONDS = 5
DEFAULT_DHASH_MAX = 14
DEFAULT_SPAN_SECONDS = 15


def get_burst_config():
    cfg, _ = Config.objects.get_or_create(id=1)
    return (
        int(cfg.burst_gap_seconds or DEFAULT_GAP_SECONDS),
        int(cfg.burst_dhash_max if cfg.burst_dhash_max is not None else DEFAULT_DHASH_MAX),
        int(cfg.burst_span_seconds or DEFAULT_SPAN_SECONDS),
    )


def params_snapshot(gap, dhash_max, span):
    return {'gap_seconds': gap, 'dhash_max': dhash_max, 'span_seconds': span}


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


def _select_scope_photos(dataset_id, time_anchors, old_group_ids, gap, span, recompute_all):
    base_qs = Photo.objects.select_for_update().filter(
        dataset_id=dataset_id, status='submitted', captured__isnull=False
    ).order_by('captured', 'id')
    if recompute_all:
        return list(base_qs)

    anchors = [t for t in (time_anchors or []) if t is not None]
    old_ids = [g for g in (old_group_ids or []) if g]
    if old_ids:
        anchors.extend(
            Photo.objects.filter(dataset_id=dataset_id, burst_group__in=old_ids, captured__isnull=False)
            .values_list('captured', flat=True)
        )
    if not anchors:
        return []

    pad = timedelta(seconds=span + gap)
    low = min(anchors) - pad
    high = max(anchors) + pad

    # Expand outward until we hit natural time gaps > gap_seconds so local result == full recompute.
    gap_delta = timedelta(seconds=gap)
    while True:
        prev_photo = (
            Photo.objects.filter(dataset_id=dataset_id, status='submitted', captured__lt=low)
            .order_by('-captured', '-id')
            .first()
        )
        if prev_photo and (low - prev_photo.captured) <= gap_delta:
            low = prev_photo.captured - pad
        else:
            break
    while True:
        next_photo = (
            Photo.objects.filter(dataset_id=dataset_id, status='submitted', captured__gt=high)
            .order_by('captured', 'id')
            .first()
        )
        if next_photo and (next_photo.captured - high) <= gap_delta:
            high = next_photo.captured + pad
        else:
            break

    return list(base_qs.filter(captured__gte=low, captured__lte=high))


def _pick_group_cover(cluster, actor, group_id, reason):
    manuals = [p for p in cluster if p.burst_cover and p.burst_cover_at is not None]
    if manuals:
        manuals.sort(key=lambda p: (-p.burst_cover_at.timestamp(), p.captured, p.id))
        winner = manuals[0]
        if len(manuals) > 1:
            Audit.objects.create(
                actor=actor,
                action='burst_cover_conflict_resolved',
                target=group_id,
                detail={
                    'reason': reason,
                    'winner': winner.id,
                    'candidates': [p.id for p in manuals],
                },
            )
        return winner, winner.burst_cover_at

    existing_auto = [p for p in cluster if p.burst_cover]
    if len(existing_auto) == 1:
        return existing_auto[0], None

    # Deterministic fallback: earliest captured, then smallest id
    ordered = sorted(cluster, key=lambda p: (p.captured, p.id))
    winner = ordered[0]
    if reason in ('correct', 'move_out', 'move_in', 'recycled', 'restored', 'recompute') or len(existing_auto) > 1:
        Audit.objects.create(
            actor=actor,
            action='burst_cover_reselected',
            target=group_id,
            detail={'reason': reason, 'winner': winner.id, 'members': [p.id for p in ordered]},
        )
    return winner, None


def reconcile_burst_scope(
    dataset_id,
    time_anchors=None,
    old_group_ids=None,
    actor=None,
    reason='submit',
    recompute_all=False,
):
    """Deterministically recompute burst groups for affected scope in a dataset."""
    with transaction.atomic():
        Dataset.objects.select_for_update().get(id=dataset_id)
        gap, dhash_max, span = get_burst_config()
        params = params_snapshot(gap, dhash_max, span)

        # Clean any stale burst flags on non-submitted or timeless photos within old_group_ids
        clean_old = [g for g in (old_group_ids or []) if g]
        if clean_old:
            Photo.objects.filter(dataset_id=dataset_id, burst_group__in=clean_old).exclude(
                status='submitted', captured__isnull=False
            ).update(burst_group='', burst_cover=False, burst_cover_at=None)

        photos = _select_scope_photos(dataset_id, time_anchors, clean_old, gap, span, recompute_all)
        if not photos:
            return {'processed': 0, 'groups': 0}

        for p in photos:
            ensure_photo_dhash(p)

        clusters = []
        current = []
        for p in photos:
            if not p.dhash:
                if current:
                    clusters.append(current)
                    current = []
                clusters.append([p])
                continue
            if not current:
                current = [p]
                continue
            prev = current[-1]
            first = current[0]
            if (
                prev.dhash
                and (p.captured - prev.captured).total_seconds() <= gap
                and (p.captured - first.captured).total_seconds() <= span
                and dhash_distance(prev.dhash, p.dhash) <= dhash_max
            ):
                current.append(p)
            else:
                clusters.append(current)
                current = [p]
        if current:
            clusters.append(current)

        before_state = {
            p.id: (p.burst_group, p.burst_cover, p.burst_cover_at, p.burst_algo, p.burst_params)
            for p in photos
        }

        used_group_ids = set(
            Photo.objects.filter(dataset_id=dataset_id, status='submitted')
            .exclude(id__in=[p.id for p in photos])
            .exclude(burst_group='')
            .values_list('burst_group', flat=True)
        )

        # Clear burst_cover first inside transaction to satisfy partial UniqueConstraint safely
        cleared_ids = {p.id for p in photos if p.burst_cover}
        if cleared_ids:
            Photo.objects.filter(id__in=cleared_ids).update(burst_cover=False)

        group_count = 0
        changed_records = []

        for cluster in clusters:
            if len(cluster) < 2:
                p = cluster[0]
                new_state = ('', False, None, BURST_ALGO, params)
                if before_state[p.id] != new_state:
                    p.burst_group = ''
                    p.burst_cover = False
                    p.burst_cover_at = None
                    p.burst_algo = BURST_ALGO
                    p.burst_params = params
                    p.save(update_fields=['burst_group', 'burst_cover', 'burst_cover_at', 'burst_algo', 'burst_params'])
                    changed_records.append((p.id, before_state[p.id][0], ''))
                continue

            group_count += 1
            counts = Counter(p.burst_group for p in cluster if p.burst_group and p.burst_group not in used_group_ids)
            if counts:
                # Pick most common existing group id, tie-break deterministically
                gid = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            else:
                gid = str(uuid.uuid4())
            used_group_ids.add(gid)

            winner, winner_at = _pick_group_cover(cluster, actor, gid, reason)
            for p in cluster:
                is_cover = p.id == winner.id
                cover_at = winner_at if is_cover else None
                new_state = (gid, is_cover, cover_at, BURST_ALGO, params)
                # If p.id was cleared from DB above and remains cover, it MUST be saved back as True.
                if before_state[p.id] != new_state or (is_cover and p.id in cleared_ids):
                    p.burst_group = gid
                    p.burst_cover = is_cover
                    p.burst_cover_at = cover_at
                    p.burst_algo = BURST_ALGO
                    p.burst_params = params
                    p.save(update_fields=['burst_group', 'burst_cover', 'burst_cover_at', 'burst_algo', 'burst_params'])
                    if before_state[p.id][0] != gid or before_state[p.id][1] != is_cover:
                        changed_records.append((p.id, before_state[p.id][0], gid))

        if changed_records and reason in ('correct', 'move_out', 'move_in', 'recycled', 'restored', 'recompute'):
            Audit.objects.create(
                actor=actor,
                action='burst_reconcile',
                target=str(dataset_id),
                detail={
                    'reason': reason,
                    'algo': BURST_ALGO,
                    'params': params,
                    'changes': [{'photo': pid, 'from': old_g, 'to': new_g} for pid, old_g, new_g in changed_records[:100]],
                },
            )

        return {'processed': len(photos), 'groups': group_count}


def assign_burst_group(photo, actor=None):
    if photo.status != 'submitted' or not photo.captured:
        return ''
    reconcile_burst_scope(photo.dataset_id, time_anchors=[photo.captured], actor=actor, reason='submit')
    photo.refresh_from_db(fields=['burst_group'])
    return photo.burst_group
