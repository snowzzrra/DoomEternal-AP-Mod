"""Minimal command processor surface used by Archipelago CommonClient.

Standalone bridge does not embed Archipelago server implementation.
"""

from __future__ import annotations

import functools
import inspect
import shlex
import typing

from Utils import async_start  # type: ignore[import-not-found]


class CommandMeta(type):
    def __new__(cls, name, bases, attrs):
        commands = attrs["commands"] = {}
        for base in bases:
            commands.update(base.commands)
        commands.update(
            {
                command_name[5:]: method
                for command_name, method in attrs.items()
                if command_name.startswith("_cmd_")
            }
        )
        for command_name, method in commands.items():
            if inspect.iscoroutinefunction(method):

                def wrapper(self, *args, _method=method, **kwargs):
                    return async_start(_method(self, *args, **kwargs))

                functools.update_wrapper(wrapper, method)
                commands[command_name] = wrapper
        return super().__new__(cls, name, bases, attrs)


ReturnType = typing.TypeVar("ReturnType")


def mark_raw(function: typing.Callable[[typing.Any], ReturnType]):
    setattr(function, "raw_text", True)
    return function


class CommandProcessor(metaclass=CommandMeta):
    commands: dict[str, typing.Callable]
    client = None
    marker = "/"

    def output(self, text: str) -> None:
        print(text)

    def __call__(self, raw: str) -> typing.Optional[bool]:
        if not raw:
            return None
        try:
            try:
                command = shlex.split(raw, comments=False)
            except ValueError:
                command = raw.split()
            base_command = command[0]
            if base_command[0] != self.marker:
                self.default(raw)
                return None
            method = self.commands.get(base_command[1:].lower())
            if method is None:
                self._error_unknown_command(base_command[1:])
                return None
            if getattr(method, "raw_text", False):
                argument = raw.split(maxsplit=1)
                return method(self, argument[1]) if len(argument) > 1 else method(self)
            return method(self, *command[1:])
        except Exception as error:
            self._error_parsing_command(error)
            return None

    def get_help_text(self) -> str:
        result = ""
        for command, method in self.commands.items():
            argument_text = ""
            for argument_name, parameter in inspect.signature(method).parameters.items():
                if argument_name == "self":
                    continue
                if isinstance(parameter.default, str):
                    argument_name = (
                        f"[{argument_name}]"
                        if not parameter.default
                        else f"{argument_name}={parameter.default}"
                    )
                argument_text += argument_name + " "
            documentation = inspect.getdoc(method) or "(missing help text)"
            indented_documentation = "\n    ".join(documentation.splitlines())
            result += (
                f"{self.marker}{command} {argument_text}\n"
                f"    {indented_documentation}\n"
            )
        return result

    def _cmd_help(self) -> None:
        """Returns the help listing."""
        self.output(self.get_help_text())

    def _cmd_license(self) -> None:
        """Returns licensing information."""
        self.output("See the licenses directory distributed with this launcher.")

    def default(self, raw: str) -> None:
        self.output("Echo: " + raw)

    def _error_unknown_command(self, raw: str) -> None:
        self.output(f"Could not find command {raw}. Known commands: {', '.join(self.commands)}")

    def _error_parsing_command(self, exception: Exception) -> None:
        import traceback

        self.output(traceback.format_exc())
