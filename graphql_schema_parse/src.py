import json
import os
import re
from abc import ABC
from abc import abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import Dict
from typing import Tuple
from typing import Text
from typing import List
from typing import Any
from typing import Union
from typing import Optional
from urllib.request import Request
from urllib.request import urlopen
from queue import Queue

from graphql import get_introspection_query
from graphql import build_schema
from graphql import build_client_schema
from graphql import GraphQLInputObjectType
from graphql import GraphQLInputField
from graphql import GraphQLArgument
from graphql import GraphQLNamedType
from graphql import GraphQLField
from graphql import GraphQLSchema
from graphql import GraphQLObjectType
from jinja2 import Template
from typer import progressbar

__all__ = ["make_action"]

VarsFieldType = Union[GraphQLField, GraphQLInputField, GraphQLArgument]

url_regx = (
    r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
)


class GraphqlDocsParse(ABC):
    scalar_default = {
        "Int": 0,
        "String": "",
        "ID": 0,
        "Date": "2022-01-24",
        "DateTime": "2022-01-24",
        "Float": 0.0,
        "Boolean": False,
        "JSON": "JSON",
    }

    # sqlmap 替换类型
    sqlmap_regx = ["String", "Date", "JSON"]

    def __init__(self):
        self.schemas: Optional[GraphQLSchema] = None
        self.json_queue = Queue()

    @abstractmethod
    def build_graphql_schema(self):
        """构建schema 文档的方法"""
        pass

    def start(self, depth: int = 1, is_sqlmap: bool = False):
        """对外暴露的解析方法"""
        if self.schemas is None:
            self.build_graphql_schema()
        self.load_query(depth, is_sqlmap=is_sqlmap)

    @staticmethod
    def query_template(
        is_type: str,
        operation_name: str,
        vars_str: str,
        resolve_str: str,
        args: List[str],
    ) -> str:
        """
        query语句模板
        :param is_type: query 语句类型，  mutation ， query, subscription
        :param operation_name: 接口名称
        :param vars_str: 顶层参数定义
        :param resolve_str: 底层使用变量参数
        :param args: query请求体查询字段列表
        :return: 完整的query 语句
        """
        gql = Template(
            """ {{ type }} {{ operationName }} {% if vars %}{{vars}}{% endif %}{
        {{operationName}}{%if res_vars %} {{res_vars}}{%endif%}{% if args%}{
                 {% for arg in args %} {{arg}}
                 {% endfor %}}
             {%endif%}
        }
        """
        ).render(
            {
                "type": is_type,
                "operationName": operation_name,
                "vars": vars_str,
                "res_vars": resolve_str,
                "args": args,
            }
        )
        return gql

    def get_return_obj(self, field_obj: GraphQLField) -> GraphQLNamedType:
        """获取到最下一层的 GraphQLObjectType"""
        field_name = str(field_obj.type)

        if (start := field_name.find("[")) != -1:
            end = field_name.find("]")
            field_name = field_name[start + 1: end]

        if field_name.endswith("!"):
            field_name = field_name[0:-1]

        return self.schemas.type_map.get(field_name)

    def get_variables(
        self,
        items: Dict[str, VarsFieldType],
        data_map: Optional[Dict[Any, Any]] = None,
        is_sqlmap: bool = False,
    ) -> Dict[str, Any]:
        """
        填充数据
        :param items: 被迭代对象
        :param data_map: 存储数据对象
        :param is_sqlmap: 是否使用sqlmap规则
        :return: 填充完成的参数字典
        """
        if data_map is None:
            data_map = {}

        for k, v in items.items():
            v_type = str(v.type)

            # 标记是否是列表
            flag = False

            if (start := v_type.find("[")) != -1:
                end = v_type.find("]")
                v_type = v_type[start + 1: end]
                flag = True

            if v_type.endswith("!"):
                v_type = v_type[0:-1]

            type_obj: Optional[GraphQLInputObjectType] = self.schemas.type_map.get(
                v_type
            )

            # Input 输入类型
            if isinstance(type_obj, GraphQLInputObjectType):
                variables = self.get_variables(type_obj.fields, data_map, is_sqlmap)
                if flag:
                    data_map = {k: [variables]}
                else:
                    data_map = {k: variables}

            # 标量类型
            elif type_obj.name in GraphqlDocsParse.scalar_default:
                if is_sqlmap and type_obj.name in GraphqlDocsParse.sqlmap_regx:
                    result = "*"
                else:
                    result = GraphqlDocsParse.scalar_default[type_obj.name]

                if flag:
                    data_map[k] = [result]
                else:
                    data_map[k] = result
            else:
                raise TypeError(f"该类型({v_type})设置默认数据哦", type(v.type))

        return data_map

    def find_fields(
        self,
        field_obj: Union[GraphQLNamedType, GraphQLObjectType],
        results: Optional[List[str]] = None,
        depth: int = 1,
    ):
        """
        递归找到query语句中可用查询字段列表
        :param field_obj: 字段对象
        :param results: 结果集列表
        :param depth: 递归次数
        :return:
        """
        if results is None:
            results = []
        for k, v in field_obj.fields.items():
            obj = self.get_return_obj(v)
            if isinstance(obj, GraphQLObjectType) and depth != 0:
                results.append("%s{" % k)
                results.extend(self.find_fields(obj, depth=depth - 1))
                results.append("}")
            elif isinstance(obj, GraphQLObjectType):
                pass
            else:
                results.append(k)
        return results

    def get_query_str(
        self, is_type: str, query_name: str, field_obj: GraphQLField, depth: int
    ) -> str:
        """
        生成query 语句
        :param is_type: 接口类型 mutation ， query, subscription
        :param query_name: 接口名称
        :param field_obj: 字段对象
        :param depth: 查找query语句体中的 字段 使用的递归层级
        :return gql: 完整的query 语句
        """

        # args query 请求体中可用字段列表
        args = []
        field_objs = self.get_return_obj(field_obj)
        if isinstance(field_objs, GraphQLObjectType):
            args.extend(self.find_fields(field_objs, depth=depth))

        # 获取参数部分query语句
        vars_str = ""  # 变量第一层及
        resolve_str = ""  # 变量 最后一层反转
        for k, v in field_obj.args.items():
            vars_str += f"${k}: {v.type}, "
            resolve_str += f"{k}: ${k}, "
        if vars_str != "":
            vars_content = f"({vars_str[0:-2]})"
            resolve_content = f"({resolve_str[0:-2]})"
        else:
            vars_content = None
            resolve_content = None

        gql = GraphqlDocsParse.query_template(
            is_type,
            operation_name=query_name,
            vars_str=vars_content,
            resolve_str=resolve_content,
            args=args,
        )
        return gql

    def load_query(self, depth: int = 1, is_sqlmap: bool = False):
        """query 查询名称， 返回字段类型名称"""
        for types_name in ["query", "mutation", "subscription"]:
            gql_type = getattr(self.schemas, f"{types_name}_type")
            if not hasattr(gql_type, "fields"):
                pass
            else:
                for query_name, query_return in gql_type.fields.items():
                    result = {
                        "query": self.get_query_str(
                            types_name, query_name, query_return, depth
                        ),
                        "variables": self.get_variables(
                            query_return.args, is_sqlmap=is_sqlmap
                        ),
                        "operationName": query_name,
                    }
                    self.json_queue.put(result)


class GraphqlDocsParseUrl(GraphqlDocsParse):
    """Graphql URL 方式解析文档"""

    def __init__(self, url: str = None, headers: Optional[Dict[str, str]] = None):
        self.url = url
        self.headers = headers
        super().__init__()

    @property
    def url(self):
        return self._url

    @url.setter
    def url(self, value: str):
        """验证"""
        if not isinstance(value, str):
            raise TypeError("url 不符合规则")
        if result := re.match(url_regx, value):
            path = result.group(0)
            if path.split("/")[-1] != "graphql":
                if path[-1] == "/":
                    value += "graphql"
                else:
                    value += "/graphql"
        self._url = value

    @property
    def headers(self):
        return self._headers

    @headers.setter
    def headers(self, value: Dict[str, str]):
        self._headers = {} if value is None else value
        self._headers.update({"Content-Type": "application/json"})

    def build_graphql_schema(self):
        """通过请求url的方式获取到 需要的 GraphQLSchema 对象"""
        data = json.dumps({"query": get_introspection_query(descriptions=True)})
        resp = Request(url=self.url, headers=self.headers, data=data.encode("utf-8"))
        data = json.loads(urlopen(resp).read())["data"]
        self.schemas = build_client_schema(data)

    def sqlmap_template(self):
        """
        返回sqlmap模板字符串
        :return:
        """
        url_host = self.url[self.url.find("//") + 2:]
        host = url_host.split("/")[0]
        url = "/".join(url_host.split("/")[1:])
        headers = json.dumps(self.headers)[1:-1].replace('"', "").split(",")
        return Template(
            "POST /{{url}} HTTP/1.1\nHOST: {{host}}\n{%for header in headers%}{{header|trim}}\n{%endfor%}"
        ).render(url=url, host=host, headers=headers)


class GraphqlDocsParseFile(GraphqlDocsParse):
    """本地文档解析"""

    def __init__(self, path: str):
        self.path = path
        super().__init__()

    @abstractmethod
    def load_file_content(self) -> Tuple[str, Union[Dict, Text]]:
        pass

    def build_graphql_schema(self):
        type_str, content = self.load_file_content()
        if type_str == "json":
            self.schemas = build_client_schema(content)
        if type_str == "gql":
            self.schemas = build_schema(content)


class GraphqlDocsParseJson(GraphqlDocsParseFile):
    def load_file_content(self) -> Tuple[str, Union[Dict, Text]]:
        """读取本地json文件"""
        with open(self.path, "r", encoding="utf-8") as f:
            content = json.load(f)
        return "json", content["data"]


class GraphqlDocsParseSchema(GraphqlDocsParseFile):
    """实际测试过程中该方法会丢失部分语句 测试过程 通过 json 文件 和url 解 共解出 310 条，该方法只解出 304条"""

    def load_file_content(self) -> Tuple[str, Union[Dict, Text]]:
        """读取本地.graphql文档"""
        with open(self.path, "r", encoding="utf-8") as f:
            content = f.read()
        return "gql", content


class MakeFile:
    """写 json 文件 写gql文件 写txt(sqlmap) 文件"""

    def __init__(self, parse_obj: GraphqlDocsParse, path: str):
        """
        初始化数据方法
        :param parse_obj: GraphqlDocsParse 解析文档对象
        :param path: 文件保存目录
        """
        self.parse_obj = parse_obj
        self.path = path
        if not os.path.isdir(path):
            os.mkdir(path)

    @abstractmethod
    def make_file(self, info: Dict[str, Any]):
        """写入文件"""
        pass

    def async_write(self):
        """线程池写文件"""
        total = 0
        qsize = self.parse_obj.json_queue.qsize()
        with ThreadPoolExecutor() as executor:
            with progressbar(range(qsize), label=f"进度") as progress:
                for _ in progress:
                    executor.submit(self.make_file, self.parse_obj.json_queue.get())
                    total += 1
        return total


class MakeGqlFile(MakeFile):
    def make_file(self, info: Dict[str, Any]):
        """写入gql文件"""
        file_path = self.path + "/" + info["operationName"] + ".gql"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(info["query"])


class MakeJsonFile(MakeFile):
    def make_file(self, info: Dict[str, Any]):
        file_path = self.path + "/" + info["operationName"] + ".json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(info, f)


class MakeSqlmapFile(MakeFile):
    def __init__(self, parse_obj: GraphqlDocsParse, path: str, template: str):
        """
        初始化数据， 待实现
        :param parse_obj:
        :param path:
        :param template: sqlmap扫描文件内容
        """
        self.template = template
        super().__init__(parse_obj, path)

    def make_file(self, info: Dict[str, Any]):
        """
        制作sqlmap -r 可用的文件
        :param info: 解析而来的json参数
        :return:
        """
        file_path = self.path + "/" + info["operationName"] + ".txt"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(self.template + "\n" + json.dumps(info))


def make_action(
    path: str,
    directory: str,
    to_type: str,
    headers: Optional[Dict[str, str]] = None,
    depth: int = 1,
):
    """
    对cli程序暴露的制作文件完整方法
    :param path: 文件（.json） / (.graphql) / url  路径
    :param directory: 保存在该目录文件下，如果不存在则会创建
    :param to_type: 可选json, gql, sqlmap
    :param headers: 当path 为 url内容时的可选项
    :param depth: 生成的query语句中最大递归深度 默认为1
    :return:
    """
    is_url = re.match(url_regx, path)
    suffix = path.split(".")[-1]

    if is_url:
        parse_obj = GraphqlDocsParseUrl(path, headers)

        if to_type == "sqlmap":
            template = parse_obj.sqlmap_template()
            parse_obj.start(depth, True)
            return MakeSqlmapFile(parse_obj, directory, template).async_write()

    elif suffix == "json":
        parse_obj = GraphqlDocsParseJson(path)
    elif suffix == "graphql":
        parse_obj = GraphqlDocsParseSchema(path)
    else:
        raise ValueError("参数错误，path 应该是个 url地址 或者 json文件 或者 graphql(SDL)文件")

    parse_obj.start(depth)

    if to_type == "json":
        return MakeJsonFile(parse_obj, directory).async_write()
    elif to_type == "gql":
        return MakeGqlFile(parse_obj, directory).async_write()
    else:
        raise TypeError("暂只支持解析成JSON，GQL，sqlmap 文件")
