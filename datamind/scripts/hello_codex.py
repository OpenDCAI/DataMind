"""Live SDK smoke using the configured main Codex agent."""
import asyncio
from datamind.config import DataSettings
from datamind.host import CodexHost
async def run():
    host=CodexHost(DataSettings())
    try:
        result=await host.query('Reply with exactly pong. Do not call tools.')
        print(result['answer'])
        if 'pong' not in result['answer'].lower():
            raise RuntimeError('Codex did not complete the smoke prompt')
    finally:
        await host.aclose()
def main():asyncio.run(run())
if __name__=='__main__':main()
