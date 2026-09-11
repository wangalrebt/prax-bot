"""CLI entry point; command implementations live under ui.commands."""

import typer

from prax_bot.ui.commands import configuration, execution, history

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="Prax Bot: a coding agent that works in a Nebius Token Factory Sandbox, powered by NVIDIA Nemotron.",
)

for command in (
    execution.session,
    execution.ui,
    execution.web,
    execution.run,
    execution.init,
    execution.goal,
    execution.resume,
):
    app.command()(command)
for command in (history.status, history.inspect, history.report):
    app.command()(command)
for command in (configuration.config, configuration.doctor, configuration.version):
    app.command()(command)
app.command(name="prompt")(configuration.prompt_command)
app.callback(invoke_without_command=True)(execution.main)
