from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from dotenv import dotenv_values
import shutil

root = Path(__file__).resolve().parent.parent
names = ['bot.py', 'collector.py', 'export_core.py', 'telegram_api.py',
         'test_bot.py', 'README.md', 'requirements.txt', 'requirements.lock',
         '.gitignore', '.env.example', 'start.ps1']
secrets = [v.encode() for k, v in dotenv_values(root / '.env').items()
           if k in ('BOT_TOKEN', 'API_HASH') and v]
out = root / 'outputs' / 'telegram-member-export-bot.zip'
out.parent.mkdir(exist_ok=True)
with ZipFile(out, 'w', ZIP_DEFLATED) as archive:
    for name in names:
        data = (root / name).read_bytes()
        assert not any(s in data for s in secrets), 'Secret in deliverable'
        archive.writestr('telegram-member-export-bot/' + name, data)
with ZipFile(out) as archive:
    assert len(archive.namelist()) == len(names)
    assert archive.testzip() is None
destination = root.parent / 'realtime-voice-chat' / 'outputs' / out.name
destination.parent.mkdir(exist_ok=True)
shutil.copy2(out, destination)
print('ZIP checked; no token/API Hash, environment or session files included.')
print(out)
print(destination)
