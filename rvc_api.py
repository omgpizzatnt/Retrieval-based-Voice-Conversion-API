#!/usr/bin/env python3
"""
RVC Inference API Server
基于原版RVC的非实时HTTP REST API服务

使用方法:
    python rvc_api.py --host 0.0.0.0 --port 8000 --device cuda --half
    python rvc_api.py --device cpu --port 8080
"""

import os
import sys
import argparse
import logging
from io import BytesIO
from typing import Optional

now_dir = os.getcwd()
sys.path.append(now_dir)

from dotenv import load_dotenv
from scipy.io import wavfile

from configs.config import Config
from infer.modules.vc.modules import VC

from fastapi import FastAPI, UploadFile, HTTPException, Query
from fastapi.responses import StreamingResponse
import uvicorn

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class ModelCache:
    def __init__(self, config: Config):
        self.models = {}
        self.config = config

    def load_model(self, model_name: str):
        if model_name not in self.models:
            logger.info(f"Loading model: {model_name}")
            vc = VC(self.config)
            possible_paths = [
                f"{model_name}.pth",
                os.path.join("logs", model_name, f"{model_name}.pth"),
                os.path.join("logs", model_name, "model.pth"),
            ]
            model_path = None
            for path in possible_paths:
                if os.path.exists(path):
                    model_path = path
                    break
            if not model_path:
                raise FileNotFoundError(f"Model file not found for: {model_name}")
            vc.get_vc(model_path)
            self.models[model_name] = vc
            logger.info(f"Model {model_name} loaded successfully")
        return self.models[model_name]

    def unload_model(self, model_name: str):
        if model_name in self.models:
            del self.models[model_name]
            import torch
            torch.cuda.empty_cache()
            logger.info(f"Model {model_name} unloaded")

    def list_loaded_models(self):
        return list(self.models.keys())


def find_index_file(model_name: str) -> Optional[str]:
    log_dir = os.path.join("logs", model_name)
    if not os.path.exists(log_dir):
        return None
    import glob
    index_files = glob.glob(os.path.join(log_dir, "*.index"))
    return index_files[0] if index_files else None


def create_app(config: Config, model_cache: ModelCache) -> FastAPI:
    app = FastAPI(
        title="RVC Inference API",
        description="非实时语音转换HTTP REST API",
        version="1.0.0"
    )

    @app.post("/convert")
    async def convert_voice(
        input_file: UploadFile,
        model_name: str = Query(..., description="模型名称"),
        f0_up_key: int = Query(0, ge=-24, le=24, description="音高调整（半音数）"),
        f0_method: str = Query("rmvpe", description="音高提取算法: rmvpe/crepe/harvest/pm"),
        index_rate: float = Query(0.75, ge=0.0, le=1.0, description="特征检索比例"),
        filter_radius: int = Query(3, ge=0, le=10, description="滤波半径"),
        resample_sr: int = Query(0, ge=0, description="输出重采样率"),
        rms_mix_rate: float = Query(0.25, ge=0.0, le=1.0, description="音量包络混合率"),
        protect: float = Query(0.33, ge=0.0, le=0.5, description="清辅音保护")
    ):
        try:
            if not input_file.filename.endswith('.wav'):
                raise HTTPException(status_code=400, detail="只支持.wav格式")
            index_path = find_index_file(model_name)
            if not index_path:
                raise HTTPException(
                    status_code=400,
                    detail=f"未找到模型 '{model_name}' 的index文件"
                )
            logger.info(f"Converting: model={model_name}, f0={f0_method}, key={f0_up_key}")
            audio_bytes = await input_file.read()
            vc = model_cache.load_model(model_name)
            _, wav_opt = vc.vc_single(
                sid=0,
                input_audio_path=audio_bytes,
                f0_up_key=f0_up_key,
                f0_file=None,
                f0_method=f0_method,
                file_index=index_path,
                file_index2=None,
                index_rate=index_rate,
                filter_radius=filter_radius,
                resample_sr=resample_sr,
                rms_mix_rate=rms_mix_rate,
                protect=protect
            )
            output_buffer = BytesIO()
            wavfile.write(output_buffer, wav_opt[0], wav_opt[1])
            output_buffer.seek(0)
            output_filename = f"converted_{input_file.filename}"
            return StreamingResponse(
                output_buffer,
                media_type="audio/wav",
                headers={"Content-Disposition": f"attachment; filename={output_filename}"}
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"转换失败: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"转换失败: {str(e)}")

    @app.get("/models")
    async def list_models():
        models = []
        logs_dir = "logs"
        if os.path.exists(logs_dir):
            for item in sorted(os.listdir(logs_dir)):
                item_path = os.path.join(logs_dir, item)
                if os.path.isdir(item_path):
                    pth_files = [f for f in os.listdir(item_path) if f.endswith('.pth')]
                    if pth_files:
                        index_files = [f for f in os.listdir(item_path) if f.endswith('.index')]
                        models.append({
                            "name": item,
                            "checkpoint": pth_files[0] if pth_files else None,
                            "index": index_files[0] if index_files else None,
                            "ready": len(index_files) > 0
                        })
        return {"models": models, "count": len(models)}

    @app.get("/models/loaded")
    async def list_loaded_models():
        return {
            "loaded_models": model_cache.list_loaded_models(),
            "count": len(model_cache.list_loaded_models())
        }

    @app.post("/models/unload")
    async def unload_model(model_name: str):
        model_cache.unload_model(model_name)
        return {"message": f"模型 '{model_name}' 已卸载"}

    @app.get("/health")
    async def health_check():
        import torch
        return {
            "status": "healthy",
            "device": str(config.device),
            "is_half": config.is_half,
            "cuda_available": torch.cuda.is_available(),
            "models_loaded": len(model_cache.list_loaded_models())
        }

    @app.get("/")
    async def root():
        return {
            "service": "RVC Inference API",
            "version": "1.0.0",
            "docs": "/docs",
            "endpoints": {
                "convert": "POST /convert",
                "models": "GET /models",
                "health": "GET /health"
            }
        }

    return app


def parse_args():
    parser = argparse.ArgumentParser(
        description="RVC Inference API Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python rvc_api.py
  python rvc_api.py --host 0.0.0.0 --port 8080
  python rvc_api.py --device cuda:0 --half
  python rvc_api.py --device cpu --port 8001
        """
    )
    parser.add_argument("--host", type=str, default="0.0.0.0", help="服务器主机地址 (默认: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="服务器端口 (默认: 8000)")
    parser.add_argument("--device", type=str, default=None, help="计算设备: cuda, cuda:0, cpu (默认: 自动检测)")
    parser.add_argument("--half", action="store_true", help="使用半精度推理 (FP16)")
    parser.add_argument("--no-half", action="store_true", help="禁用半精度推理 (使用FP32)")
    return parser.parse_args()


def main():
    args = parse_args()
    print("="*60)
    print("RVC Inference API Server")
    print("="*60)
    config = Config()
    if args.device:
        config.device = args.device
        print(f"Device: {args.device}")
    else:
        print(f"Device: {config.device} (auto)")
    if args.no_half:
        config.is_half = False
        print("Precision: FP32")
    elif args.half:
        config.is_half = True
        print("Precision: FP16")
    else:
        print(f"Precision: {'FP16' if config.is_half else 'FP32'} (auto)")
    print(f"Host: {args.host}")
    print(f"Port: {args.port}")
    print("="*60)
    model_cache = ModelCache(config)
    app = create_app(config, model_cache)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
