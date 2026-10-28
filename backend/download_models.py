# Source Generated with Decompyle++
# File: recovered/evidence/download_models.pyc (Python 3.12)

'''Tai san cac model AI vao backend/ai_models de dong goi cung ban build.

Chay 1 lan tren may BUILD truoc khi electron-builder dong goi
(xem package.json -> script "electron:build:models"). Sau khi chay, model duoc
dinh kem qua extraResources va app chay OFFLINE (local_files_only), nguoi dung
cuoi khong phai tai ~150MB lan dau.

Luu y: dung print ASCII de tranh UnicodeEncodeError tren console cp932 (Win JP).
'''
import os
import sys
import shutil
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(BACKEND_DIR, 'ai_models')
WHISPER_SIZE = os.environ.get('SUBFORGE_WHISPER_SIZE', 'base').strip() or 'base'

def cleanup_hf_cache():
    'Xoa cac thu muc .cache cua HuggingFace de ban dong goi gon hon.'
    for root, dirs, _ in os.walk(MODELS_DIR):
        if '.cache' in dirs:
            shutil.rmtree(os.path.join(root, '.cache'), ignore_errors=True)


def download_whisper():
    out = os.path.join(MODELS_DIR, 'whisper', WHISPER_SIZE)
    if os.path.isfile(os.path.join(out, 'model.bin')):
        print(f"[MODELS] Whisper '{WHISPER_SIZE}' already present, skip: {out}")
        return None
    os.makedirs(out, exist_ok=True)
    from faster_whisper import download_model
    print(f"[MODELS] Downloading faster-whisper '{WHISPER_SIZE}' -> {out}")
    download_model(WHISPER_SIZE, output_dir=out)
    print('[MODELS] Whisper done.')

DEMUCS_MODELS = ['htdemucs', 'hdemucs_mmi']

def download_demucs():
    'Tai cac model tach nguon Demucs va gom ve ai_models/demucs.\n\n    Demucs load model theo "bag": file yaml (liet ke signature checkpoint, nam san\n    trong package demucs/remote) + checkpoint .th (tai qua torch.hub ve\n    TORCH_HOME/hub/checkpoints). Gom yaml + .th cua CA HAI model vao 1 thu muc de\n    runtime load offline bang get_model(<name>, repo=<dir nay>).\n    '
    import glob as _glob
    import yaml as _yaml
    out = os.path.join(MODELS_DIR, 'demucs')
    have_all = all(os.path.isfile(os.path.join(out, f'{name}.yaml')) for name in DEMUCS_MODELS) and _glob.glob(os.path.join(out, '*.th'))
    if have_all:
        print(f'[MODELS] Demucs models already present, skip: {out}')
        return None
    os.makedirs(out, exist_ok=True)
    import torch
    from demucs.pretrained import REMOTE_ROOT, _parse_remote_files
    remote = str(REMOTE_ROOT)
    sig_urls = _parse_remote_files(REMOTE_ROOT / 'files.txt')
    for name in DEMUCS_MODELS:
        print(f"[MODELS] Downloading demucs '{name}'...")
        shutil.copy2(os.path.join(remote, f'{name}.yaml'), os.path.join(out, f'{name}.yaml'))
        with open(os.path.join(remote, f'{name}.yaml'), 'r', encoding='utf-8') as f:
            spec = _yaml.safe_load(f)
        sigs = [str(s) for s in (spec.get('models', []) or [])]
        for sig in sigs:
            if _glob.glob(os.path.join(out, f'{sig}*.th')):
                continue
            url = sig_urls.get(sig)
            if not url:
                raise RuntimeError(f'Signature {sig} cua {name} khong co trong remote/files.txt')
            torch.hub.load_state_dict_from_url(url, model_dir=out, map_location='cpu', check_hash=True)
        for sig in sigs:
            if _glob.glob(os.path.join(out, f'{sig}*.th')):
                continue
            raise RuntimeError(f'Khong tai duoc checkpoint {sig}*.th cho {name} vao {out}')
    print('[MODELS] Demucs done.')
def download_vieneu():
    'Tai toan bo asset VieNeu-TTS (v3 Turbo, ONNX) vao ai_models/hf de chay OFFLINE.\n\n    VieNeu tai nhieu file tu HuggingFace theo nhu cau (model chinh + denoiser +\n    speaker_encoder cho clone + watermark). Ta ep HF_HOME tro vao ai_models/hf roi:\n      1) khoi tao model  -> tai asset inference\n      2) infer 1 preset  -> chac chan asset acoustic/decode ve du\n      3) add_voice + infer clone -> tai denoiser + speaker_encoder (chi dung khi clone)\n    Runtime (vieneu_service) se set HF_HOME + HF_HUB_OFFLINE tro vao dung dir nay.\n    '
    hf_dir = os.path.join(MODELS_DIR, 'hf')
    marker = os.path.join(hf_dir, '.vieneu_ready_v2')
    if os.path.isfile(marker):
        print(f'[MODELS] VieNeu already present, skip: {hf_dir}')
        return None
    os.makedirs(hf_dir, exist_ok=True)
    os.environ['HF_HOME'] = hf_dir
    os.environ.pop('HF_HUB_OFFLINE', None)
    hub_cache = os.path.join(hf_dir, 'hub')
    try:
        import huggingface_hub.constants as _hf_c
        _hf_c.HF_HOME = hf_dir
        _hf_c.HF_HUB_CACHE = hub_cache
        if hasattr(_hf_c, 'HUGGINGFACE_HUB_CACHE'):
            _hf_c.HUGGINGFACE_HUB_CACHE = hub_cache
        _hf_c.HF_HUB_OFFLINE = False
    except Exception as e:
        print(f'[MODELS] Canh bao: khong va duoc HF cache constants: {e}')
    print('[MODELS] Downloading VieNeu-TTS v3 Turbo (ONNX, ~1GB)...')
    import numpy as np
    import soundfile as sf
    from vieneu import Vieneu
    try:
        from huggingface_hub import snapshot_download
        print('[MODELS] Downloading VieNeu base model snapshot...')
        snapshot_download('pnnbao-ump/VieNeu-TTS-v3-Turbo', cache_dir=hub_cache)
        print('[MODELS] VieNeu repo chinh: da tai tron 1 revision (onnx + pytorch dong bo).')
        print('[MODELS] Downloading MOSS Audio Tokenizer (ONNX + PyTorch)...')
        snapshot_download('OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX', cache_dir=hub_cache)
        snapshot_download('OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano', cache_dir=hub_cache)
    except Exception as e:
        print(f'[MODELS] Canh bao: snapshot_download loi: {e}')
    tts = Vieneu(mode='v3turbo', backend='onnx', device='cpu')
    preset = tts.list_preset_voices()[0][1]
    audio = tts.infer('Xin chào, đây là bản kiểm tra đóng gói.', voice=preset)
    ref_wav = os.path.join(hf_dir, '_warm_ref.wav')
    sf.write(ref_wav, np.asarray(audio), 24000)
    try:
        tts.add_voice('_warm_clone', ref_wav)
        tts.infer('Kiểm tra nhân bản giọng.', voice='_warm_clone')
    except Exception as e:
        print(f'[MODELS] Canh bao: khong warm duoc clone asset: {e}')
    try:
        print('[MODELS] Downloading VieNeu PyTorch backend (cho GPU pack)...')
        tts_pt = Vieneu(mode='v3turbo', backend='pytorch', device='cpu')
        tts_pt.infer('Kiểm tra bản PyTorch.', voice=preset)
        try:
            tts_pt.add_voice('_warm_clone_pt', ref_wav)
            tts_pt.infer('Kiểm tra clone PyTorch.', voice='_warm_clone_pt')
            print('[MODELS] VieNeu PyTorch clone enrollment: ok')
        except Exception as e:
            print(f'[MODELS] Canh bao: khong warm clone PyTorch: {e}')
        del tts_pt
    except Exception as e:
        print(f'[MODELS] Canh bao: khong tai duoc PyTorch backend (TTS-GPU se tu lui CPU): {e}')
    finally:
        try:
            os.remove(ref_wav)
        except Exception:
            pass
    _prune_old_hf_snapshots(hf_dir)
    _flatten_hf_symlinks(hf_dir)
    with open(marker, 'w', encoding='utf-8') as f:
        f.write('ok')
    print('[MODELS] VieNeu done.')
def _prune_old_hf_snapshots(hf_dir):
    'Moi repo HF: xoa cac snapshot KHONG duoc ref (main/...) tro toi.\n\n    Repo doi revision -> snapshot cu thanh mo coi nhung van chiem cho (sau khi\n    flatten thanh file that). Chi giu snapshot ma refs/* dang tro toi.\n    '
    hub = os.path.join(hf_dir, 'hub')
    if not os.path.isdir(hub):
        return None
    pruned = 0
    for repo in os.listdir(hub):
        repo_dir = os.path.join(hub, repo)
        snaps_dir = os.path.join(repo_dir, 'snapshots')
        refs_dir = os.path.join(repo_dir, 'refs')
        if not os.path.isdir(snaps_dir) or not os.path.isdir(refs_dir):
            continue
        keep = set()
        for rroot, _d, rfiles in os.walk(refs_dir):
            for rf in rfiles:
                try:
                    with open(os.path.join(rroot, rf), 'r', encoding='utf-8') as fh:
                        keep.add(fh.read().strip())
                except Exception:
                    continue
        for snap in os.listdir(snaps_dir):
            if snap not in keep:
                shutil.rmtree(os.path.join(snaps_dir, snap), ignore_errors=True)
                pruned += 1
    if pruned:
        print(f'[MODELS] Da don {pruned} snapshot revision cu (giam dung luong).')
        return None
    return None
def _flatten_hf_symlinks(hf_dir):
    "Bien HF cache (co symlink) thanh thu muc phang chi gom file that.\n\n    1) Thay moi symlink bang ban copy file that (deref).\n    2) Xoa cac thu muc 'blobs' (khong con can vi snapshots da la file that).\n    HF van load offline binh thuong tu snapshots.\n    "
    fixed = 0
    for root, _dirs, files in os.walk(hf_dir):
        for n in files:
            p = os.path.join(root, n)
            if not os.path.islink(p):
                continue
            tgt = os.path.realpath(p)
            os.unlink(p)
            shutil.copy2(tgt, p)
            fixed += 1
    removed = 0
    for root, _dirs, _files in list(os.walk(hf_dir)):
        if not os.path.basename(root) == 'blobs':
            continue
        shutil.rmtree(root, ignore_errors = True)
        removed += 1
    print(f'''[MODELS] VieNeu flatten: deref {fixed} symlink, xoa {removed} blobs dir.''')

if __name__ == '__main__':
    print(f'[MODELS] Target dir: {MODELS_DIR}')
    try:
        download_whisper()
        download_demucs()
        download_vieneu()
        cleanup_hf_cache()
        print('[MODELS] DONE - ready to package.')
    except Exception as e:
        print(f'[MODELS] ERROR: {e}')
        sys.exit(1)


