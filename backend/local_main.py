"""New local launcher around the recompiled and verified main.py source.

All application modules are ordinary .py imports. Evidence archives are not
opened or executed by this launcher.
"""
from pathlib import Path
import json
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from recovery_runtime import bootstrap
bootstrap()
from services.local_policy import install_network_guard
install_network_guard()
from services.local_integration import install_local_services
install_local_services()

import main as original_main
from services.local_api import add_local_routes
app = original_main.app
add_local_routes(app)


if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('SUBFORGE_LOCAL_PORT', '18080'))
    port_file = Path(original_main.get_bootstrap_dir()) / 'port.json'
    port_file.parent.mkdir(parents=True, exist_ok=True)
    port_file.write_text(json.dumps({'host': '127.0.0.1', 'port': port}), encoding='utf-8')
    uvicorn.run(app, host='127.0.0.1', port=port, reload=False)
