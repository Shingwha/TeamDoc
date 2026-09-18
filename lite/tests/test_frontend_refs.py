"""跨模块引用的静态检查:调了 `Mod.fn()` 但 fn 根本没导出。

为什么单独有这个测试:前端没有构建步骤,模块之间靠 `window.X = (function(){...})()`
挂载,**"函数写好了但忘了加进 return"不会被任何东西发现** —— 它只在用户点到那个
入口时才炸成 `X.fn is not a function`。这类错误在收敛式重构里尤其容易发生:
把散落的手写调用集中到共享模块后,调用点数量涨了一个量级,而每个新调用点都是一次
"名字必须对得上"的赌注。

判据刻意宽松:导出面 = 导出对象里的键 **或** 模块名下被赋值过的成员(`Mod.x = …`)。
所以它只抓"这个名字在模块上根本不存在"——够抓漏导出,又不会把属性式接口误报成错误。

纯静态解析(不启服务、不开浏览器),所以永远会跑。
"""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


def _read(name: str) -> str:
    return (WEB / "js" / name).read_text(encoding="utf-8")


def _js_files():
    return sorted(p for p in (WEB / "js").rglob("*.js") if "vendor" not in p.parts)


_STRINGS = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|`(?:\\.|[^`\\])*`', re.S)
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _strip(src: str) -> str:
    """去掉注释与字符串字面量 —— 之后再做括号配平才不会被 `{`/`,` 骗到。"""
    src = _BLOCK_COMMENT.sub(" ", src)
    src = _LINE_COMMENT.sub(" ", src)
    return _STRINGS.sub('""', src)


def _balanced_block(src: str, anchor: str, *, last: bool = False) -> str:
    """取 anchor 之后第一个平衡花括号块的内容(已去注释与字符串)。"""
    idx = src.rfind(anchor) if last else src.index(anchor)
    start = src.index("{", idx)
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start + 1:i]
    raise AssertionError(f"{anchor} 的括号不平衡")


def _object_keys(block: str) -> set:
    """对象字面量的键:`a: x` / 方法简写 `a(x)` / 一行多个键都算。"""
    keys, depth, cur = set(), 0, ""
    for c in block:
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            keys.update(_key_of(cur))
            cur = ""
            continue
        cur += c
    keys.update(_key_of(cur))
    return keys


def _key_of(part: str) -> set:
    m = re.match(r"\s*([A-Za-z_$][\w$]*)\s*[:(]", part)
    return {m.group(1)} if m else set()


# 模块 → (文件, 取导出对象用的锚点)。新增模块时在这里补一行。
MODULES = {
    "Endpoints": ("endpoints.js", "return {"),
    "Preview": ("preview.js", "return {"),
    "Ref": ("ref.js", "return {"),
    "UI": ("ui.js", "return {"),
    # MdRender 与 FilesAPI 都是直接赋对象字面量,没有 IIFE 的 return
    "MdRender": ("markdown.js", "window.MdRender = {"),
    "Diff": ("diff.js", "return {"),
    "Recent": ("views/recent.js", "return {"),
    "MoveTarget": ("views/move-target.js", "return {"),
    "FilesAPI": ("views/drive.js", "window.FilesAPI = {"),
    "Route": ("route.js", "return {"),
    "Scope": ("scopes.js", "return {"),
    "App": ("app.js", "const App = {"),
}


def _exports() -> dict:
    out = {}
    for mod, (path, anchor) in MODULES.items():
        src = _strip(_read(path))
        names = _object_keys(_balanced_block(src, anchor, last=anchor == "return {"))
        # 事后挂载的成员:任何模块都可以在定义之后补挂(如 App.refresh = …)
        for js in _js_files():
            names |= set(re.findall(rf"\b{mod}\.([A-Za-z_$][\w$]*)\s*=", _strip(js.read_text(encoding="utf-8"))))
        out[mod] = names
    return out


def _calls(src: str) -> set:
    """源码里的 `Mod.fn(` 调用(已去注释与字符串,免得把文档示例当成调用)。"""
    return set(re.findall(r"\b([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\(", src))


def test_no_call_to_unexported_member():
    """全站 `Mod.fn(` 的每个 fn 都必须在该模块的导出面上。"""
    exports = _exports()
    problems = []
    for js in _js_files():
        for mod, used in _calls(_strip(js.read_text(encoding="utf-8"))):
            if mod in exports and used not in exports[mod]:
                problems.append(f"{js.relative_to(WEB).as_posix()}: {mod}.{used}() 未导出")
    assert not problems, "跨模块调用了未导出的成员:\n" + "\n".join(f"  - {p}" for p in sorted(problems))


def test_export_surfaces_are_parsed():
    """自检:解析不出名字说明解析逻辑失效(那时上面的测试会变成永远通过)。

    光看数量不够(模块间导出规模差很大,而且"解析到的其实是别的块"数量也正常),
    所以再钉几个**确定存在的名字**:它们在就说明每个模块取到了对的块。
    """
    exports = _exports()
    for mod, names in exports.items():
        assert names, f"{mod} 一个导出名都没解析出来,解析逻辑可能失效"
    must_have = [("Endpoints", "projects"), ("Endpoints", "fileMeta"), ("Preview", "kindOf"),
                 ("Preview", "open"), ("MdRender", "mount"), ("MdRender", "render"),
                 ("UI", "toast"), ("UI", "canEdit"), ("Ref", "parse"), ("Ref", "downloadUrl"),
                 ("Diff", "build"), ("App", "route"), ("FilesAPI", "upload"), ("Recent", "fetch"),
                 ("Route", "resolve"), ("Scope", "html"), ("Scope", "apply")]
    for mod, name in must_have:
        assert name in exports[mod], f"{mod}.{name} 没被解析到 —— 取错了块或导出面真的缺了它"
