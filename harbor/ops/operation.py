

from time import time
from typing import Any

from harbor.lib.harbor import HarborCtx
from harbor.lib.util import Conn


class BaseOp:
    name: str
    description: str

    # @classmethod
    # def call(cls, args: dict[str, str], ctx: HarborCtx, conn: Conn) -> None:





class SleepOp(BaseOp):
    name = "sleep"
    description = "Sleep for a given number of seconds"

    def init(self, **kwargs) -> None:
        # don't run the operation if this raises a ValueError or other exception.
        self.seconds = int(kwargs["seconds"])
        self.say_name = kwargs.get("say_name", "stranger")
    
    def run(self, ctx: HarborCtx, conn: Conn) -> None:
        print(f"Hello, {self.say_name}! Sleeping for {self.seconds} seconds...")
        time.sleep(self.seconds)
        print(f"Done sleeping for {self.seconds} seconds!")


