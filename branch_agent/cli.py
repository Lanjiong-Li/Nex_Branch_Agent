"""Operational entry points for local or split API/worker deployment."""
import argparse
import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv
from .schemas import ROOT
from .storage import Store


def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['migrate','worker','serve']);args=parser.parse_args()
    load_dotenv(ROOT/'.env',override=False)
    if args.command=='serve':
        import uvicorn
        uvicorn.run('branch_agent.app:create_app',factory=True,host=os.getenv('BRANCH_HOST','127.0.0.1'),
                    port=int(os.getenv('BRANCH_PORT','8767')))
        return
    store=Store(os.getenv('DATABASE_URL','postgresql:///branch_agent_local'),Path(os.getenv('BRANCH_DATA_DIR',ROOT/'.data'))/'blobs')
    store.migrate()
    if args.command=='migrate':
        print('数据库迁移完成。');store.close();return
    async def work():
        from .configuration import ConfigService
        from .model_service import ModelService
        from .engine import Engine
        engine=Engine(store,ModelService(store),ConfigService(store))
        await engine.start()
        try:await asyncio.Event().wait()
        finally:await engine.stop();store.close()
    try:asyncio.run(work())
    except KeyboardInterrupt:pass

if __name__=='__main__':main()
