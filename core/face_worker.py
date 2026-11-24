import os
import json
import hashlib
from pathlib import Path
import sys
import socket
import contextlib

ROOT=Path(__file__).resolve().parent.parent
HASHES={'face_detection_yunet_2023mar.onnx':'8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4','face_recognition_sface_2021dec.onnx':'0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79'}

def initialize():
    import cv2
    import numpy as np
    from PIL import Image,ImageOps
    if cv2.__version__!='4.12.0':raise RuntimeError('OpenCV 版本不符')
    for name,expected in HASHES.items():
        with (ROOT/'models/face'/name).open('rb') as source:
            if hashlib.file_digest(source,'sha256').hexdigest()!=expected:raise RuntimeError('模型權重 SHA-256 不符')
    def offline(*args,**kwargs):raise RuntimeError('離線人臉模型禁止網路連線')
    socket.socket.connect=socket.socket.connect_ex=offline;socket.create_connection=offline
    cv2.setNumThreads(2)
    # Load through Python buffers: OpenCV's filename loader fails on Chinese Windows paths.
    detector=cv2.FaceDetectorYN.create('onnx',np.frombuffer((ROOT/'models/face/face_detection_yunet_2023mar.onnx').read_bytes(),dtype=np.uint8),np.empty(0,dtype=np.uint8),(320,320),.9,.3,500)
    recognizer=cv2.FaceRecognizerSF.create('onnx',np.frombuffer((ROOT/'models/face/face_recognition_sface_2021dec.onnx').read_bytes(),dtype=np.uint8),np.empty(0,dtype=np.uint8))
    def infer(path):
        with Image.open(path) as original:
            if original.width*original.height>40000000:raise ValueError('圖片超過 4000 萬像素')
            image=ImageOps.exif_transpose(original).convert('RGB');image.thumbnail((1600,1600))
            image=cv2.cvtColor(np.array(image),cv2.COLOR_RGB2BGR)
        height,width=image.shape[:2];detector.setInputSize((width,height));_,faces=detector.detect(image)
        if faces is None:return []
        if len(faces)>50:raise ValueError('人臉超過 50 張，未建立此照片索引')
        results=[]
        for face in sorted(faces,key=lambda f:(float(f[0]),float(f[1]))):
            x,y,w,h=map(float,face[:4]);left=max(0,int(x));top=max(0,int(y));right=min(width,int(x+w));bottom=min(height,int(y+h))
            crop=image[top:bottom,left:right];blur=float(cv2.Laplacian(cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY),cv2.CV_64F).var()) if crop.size else 0
            usable=w>=32 and h>=32 and blur>=15
            quality={'usable':usable,'confidence':round(float(face[-1]),5),'blur':round(blur,2),'width':round(w),'height':round(h),'reason':'' if usable else '人臉過小或模糊，請提供較清晰照片'}
            feature=[]
            if usable:
                aligned=recognizer.alignCrop(image,face);v=recognizer.feature(aligned).reshape(-1);feature=(v/np.linalg.norm(v)).astype(float).tolist()
            results.append({'bbox':[left/width,top/height,max(0,right-left)/width,max(0,bottom-top)/height],'vector':feature,'quality':quality})
        return results
    return infer

if __name__=='__main__':
    try:
        with contextlib.redirect_stdout(sys.stderr):infer=initialize()
    except Exception:
        import traceback
        traceback.print_exc(file=sys.stderr);print(json.dumps({'error':'人臉模型載入失敗，請查看本機模型記錄'}),flush=True);sys.exit(1)
    for line in sys.stdin:
        try:
            request=json.loads(line);images=[]
            for row in request.get('images',[]):
                path=Path(row['path']).resolve()
                if not path.is_relative_to(Path(os.environ.get('PHOTO_MEDIA_ROOT',str(ROOT/'storage'))).resolve()):raise ValueError('圖片不在系統儲存範圍')
                try:images.append({'id':row['id'],'faces':infer(path)})
                except Exception:images.append({'id':row['id'],'error':'無法讀取、解碼或辨識此圖片，請檢查原圖與品質'})
            result={'images':images}
        except Exception:result={'error':'本機人臉推論失敗'}
        print(json.dumps(result,ensure_ascii=False,allow_nan=False),flush=True)
