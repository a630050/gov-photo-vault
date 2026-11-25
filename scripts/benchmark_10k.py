"""Batch 2 >= 10,000-photo benchmark (API payload size, search latency, clustering latency)."""
import json
import os
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / '.vendor'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings')


def main():
    tmp = tempfile.NamedTemporaryFile(suffix='.sqlite3', delete=False)
    tmp.close()
    try:
        from django.conf import settings
        settings.DATABASES['default']['NAME'] = tmp.name

        import django
        django.setup()
        from django.core.management import call_command
        call_command('migrate', verbosity=0)

        from django.contrib.auth.models import User
        from django.test import Client
        from django.utils import timezone
        from core.models import Department, Membership, Item, Dataset, Photo

        dep = Department.objects.create(name='壓測水利科')
        user = User.objects.create_user('bench', password='p')
        Membership.objects.create(
            user=user,
            department=dep,
            role='supervisor',
            permissions={'upload': True, 'organize': True, 'download': True, 'export': True, 'analyze': True},
        )
        item = Item.objects.create(department=dep, name='巡查表', fields=[{'key': 'note', 'label': '說明', 'type': 'text'}])
        ds = Dataset.objects.create(item=item, name='萬張測試集', creator=user)

        now = timezone.now()
        total_photos = 10500
        batch = []
        for i in range(total_photos):
            lat = 22.5 + (i % 100) * 0.025
            lon = 120.2 + (i // 100) * 0.015
            batch.append(
                Photo(
                    dataset=ds,
                    uploader=user,
                    name=f'bench_{i:05d}.jpg',
                    sha=f'benchsha{i:05d}',
                    status='submitted',
                    captured=now - timedelta(seconds=i * 15),
                    time_confirmed=True,
                    lat=lat,
                    lon=lon,
                    admin_county='臺北市' if i % 2 == 0 else '高雄市',
                    admin_town='信義區' if i % 2 == 0 else '前鎮區',
                    tags=['巡查'] if i % 3 == 0 else ['工程'],
                    attrs={'note': f'record-{i}'},
                    form_snapshot=item.fields,
                )
            )
        Photo.objects.bulk_create(batch, batch_size=1000)

        client = Client()
        client.force_login(user)

        t0 = time.perf_counter()
        st = client.get('/api/state')
        t_state = (time.perf_counter() - t0) * 1000
        state_bytes = len(st.content)
        state_json = json.loads(st.content)

        t1 = time.perf_counter()
        s1 = client.post('/api/search', json.dumps({'limit': 100, 'offset': 0}), content_type='application/json')
        t_search_p1 = (time.perf_counter() - t1) * 1000
        search_p1_bytes = len(s1.content)
        s1_json = json.loads(s1.content)

        t2 = time.perf_counter()
        s50 = client.post('/api/search', json.dumps({'limit': 100, 'offset': 5000}), content_type='application/json')
        t_search_p50 = (time.perf_counter() - t2) * 1000

        t3 = time.perf_counter()
        s_tag = client.post('/api/search', json.dumps({'tags': ['巡查'], 'limit': 100, 'offset': 0}), content_type='application/json')
        t_search_tag = (time.perf_counter() - t3) * 1000
        s_tag_json = json.loads(s_tag.content)

        t4 = time.perf_counter()
        mc = client.post(
            '/api/map_clusters',
            json.dumps({'bounds': {'north': 25.5, 'south': 22.0, 'east': 122.0, 'west': 120.0}, 'zoom': 11, 'grid_px': 55}),
            content_type='application/json',
        )
        t_cluster = (time.perf_counter() - t4) * 1000
        mc_json = json.loads(mc.content)

        report = {
            'total_photos': total_photos,
            'state_status': st.status_code,
            'state_ms': round(t_state, 2),
            'state_payload_bytes': state_bytes,
            'state_has_photos_key': 'photos' in state_json,
            'search_page1_ms': round(t_search_p1, 2),
            'search_page1_bytes': search_p1_bytes,
            'search_page1_returned': len(s1_json['photos']),
            'search_page1_total': s1_json['total'],
            'search_page50_ms': round(t_search_p50, 2),
            'search_tag_ms': round(t_search_tag, 2),
            'search_tag_total': s_tag_json['total'],
            'map_clusters_ms': round(t_cluster, 2),
            'map_clusters_count': len(mc_json['clusters']),
            'map_clusters_total_located': mc_json['total_located'],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        try:
            from django.db import connections
            connections.close_all()
        except Exception:
            pass
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)


if __name__ == '__main__':
    main()
