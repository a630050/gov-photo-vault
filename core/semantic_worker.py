import os
"""Run using the project's Python 3.12 model environment, never Django's interpreter."""
import hashlib
import json
import socket
import sys
from pathlib import Path
import contextlib

ROOT=Path(__file__).resolve().parent.parent
EXPECTED_SHA='b196ee3ee528b70be1158ab1aafb1d2f1c801ad2d9ffb3bae31b0d305f82fc88'

def initialize():
    weights=ROOT/'models/chinese-clip-rn50/clip_cn_rn50.pt'
    with weights.open('rb') as source:
        if hashlib.file_digest(source,'sha256').hexdigest()!=EXPECTED_SHA:raise RuntimeError('模型權重雜湊不符，停止載入')
    # Imports and diagnostics go to stderr; stdout is reserved for JSON messages.
    import torch
    from PIL import Image,ImageOps
    from opencc import OpenCC
    import cn_clip.clip as clip
    import importlib.metadata
    if importlib.metadata.version('cn-clip')!='1.6.0':raise RuntimeError('模型程式版本不符')
    vocab=Path(clip.__file__).with_name('vocab.txt')
    if hashlib.sha256(vocab.read_bytes()).hexdigest()!='45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c':raise RuntimeError('模型詞庫雜湊不符')
    def offline(*args,**kwargs):raise RuntimeError('離線模型禁止網路連線')
    socket.socket.connect=offline;socket.create_connection=offline
    torch.set_num_threads(2)
    model,preprocess=clip.load_from_name(str(weights),device='cpu',vision_model_name='RN50',text_model_name='RBT3-chinese',input_resolution=224)
    model.eval();convert=OpenCC('t2s')
    def normalize(tensor):return (tensor/tensor.norm(dim=-1,keepdim=True)).float().cpu().tolist()[0]
    def infer(request):
        with torch.inference_mode():
            if 'text' in request:
                text=convert.convert(request['text'])
                # Match the tokenizer context length instead of silently truncating.
                if len(clip._tokenizer.tokenize(text))>50:raise ValueError('關鍵字過長，請縮短至約 50 個中文字以內')
                return {'vector':normalize(model.encode_text(clip.tokenize([text])))}
            result=[]
            for row in request.get('images',[]):
                path=Path(row['path']).resolve()
                if not path.is_relative_to(Path(os.environ.get('PHOTO_MEDIA_ROOT',str(ROOT/'storage'))).resolve()):raise ValueError('圖片不在系統儲存範圍')
                try:
                    with Image.open(path) as image:
                        inputs=preprocess(ImageOps.exif_transpose(image).convert('RGB')).unsqueeze(0)
                        result.append({'id':row['id'],'vector':normalize(model.encode_image(inputs))})
                except (OSError,ValueError,Image.DecompressionBombError):result.append({'id':row['id'],'error':'無法讀取或解碼原始照片'})
            return {'images':result}
    return infer

if __name__=='__main__':
    try:
        with contextlib.redirect_stdout(sys.stderr):infer=initialize()
    except Exception:
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({'error':'離線模型無法載入，請查看本機模型記錄'}),flush=True);sys.exit(1)
    for line in sys.stdin:
        try:
            request=json.loads(line)
            with contextlib.redirect_stdout(sys.stderr):result=infer(request)
        except ValueError as e:result={'error':str(e)}
        except Exception:
            import traceback
            traceback.print_exc(file=sys.stderr);result={'error':'本機模型推論失敗，請稍後重試'}
        print(json.dumps(result,ensure_ascii=False,allow_nan=False),flush=True)
