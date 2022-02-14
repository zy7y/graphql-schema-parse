import json
from enum import Enum
from typing import Optional

from typer import confirm
from typer import launch
from typer import Typer
from typer import Option
from typer import Argument
from typer import secho
from typer import colors

from .src import make_action

app = Typer(help="- Graphql接口文档转JSON(用于HTTP请求)/GQL(Query语句)/sqlmap扫描文件 -")


class ToType(str, Enum):
    """转出类型"""

    to_json = "json"
    to_jgl = "gql"
    to_sqlmap = "sqlmap"
    to_burp = "burp"


@app.command("docs")
def visit_docs():
    """
    访问tablefill文档
    :return:
    """
    visit = confirm("即将访问文档，确认?")
    if visit:
        launch("https://github.com/zy7y/graphql-schema-parse")


@app.command("parse")
def make_cli(
    from_path: str = Argument(
        ...,
        help="接口文档地址, 本地JSON文件地址(.json) 或者 本地 SDL文件(.schema ), 或者 服务器URL填入(服务器的IP:PORT)",
    ),
    headers: Optional[str] = Option(None, help="url方式获取接口文档时，可选项传入请求头json文件地址"),
    to: ToType = Option(ToType.to_json, case_sensitive=False),
    depth: int = Option(1, help="query语句体中可用查询字段递归深度"),
    to_directory: str = Argument(..., help="生成文件保存目录，不存在时，自动创建"),
):
    """
    将Graphql接口文档转成gql文件/Json文件
    :param from_path: 接口文档地址, 本地JSON文件地址(.json) 或者 本地 SDL文件(.schema ), 或者 服务器URL填入(服务器的IP:PORT)
    :param to: 转换之后的文件类型, 可选 to_json(.json) / to_gql(.gql) / to_sqlmap(.txt)/ to_burp(.txt)
    :param headers: from_type 为url时可选项，请求头文件地址(.json)
    :param depth: query语句体中可用查询字段递归深度
    :param to_directory: 转换之后文件，保存目录
    :return:
    """
    secho("任务开始", fg=colors.YELLOW)
    if headers and headers.split(".")[-1].lower() == "json":
        with open(headers, "r", encoding="utf-8") as f:
            headers = json.load(f)

    total = make_action(
        path=from_path,
        headers=headers,
        to_type=to,
        depth=depth,
        directory=to_directory,
    )
    secho(f"任务完成，共计新建{total} 个文件", fg=colors.GREEN)


if __name__ == "__main__":
    app()
