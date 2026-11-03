"""Query the seeded enterprise profile with the official main Codex host."""
import asyncio
from datamind.config import DataSettings
from datamind.host import CodexHost
async def run():
    cfg=DataSettings();cfg.data.profile='enterprise_demo'
    host=CodexHost(cfg)
    try:
        result=await host.query('Inspect the available data and summarize the enterprise projects with evidence.')
        print(result['answer'])
    finally:await host.aclose()
def main():asyncio.run(run())
if __name__=='__main__':main()
