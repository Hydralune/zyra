from __future__ import annotations

import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Pt, RGBColor


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "source" / "ZYRA参赛技术论文第一版.md"
ASSETS = ROOT / "assets"
OUTPUT = ROOT / "output"
DOCX_PATH = OUTPUT / "ZYRA面向超长程复杂任务的动态异构多智能体协同系统第一版.docx"

FONT_BODY = "SimSun"
FONT_HEAD = "SimHei"
FONT_LATIN = "Times New Roman"
FONT_MONO = "Consolas"
FONT_CN_REG = Path(r"C:\Windows\Fonts\msyh.ttc")
FONT_CN_BOLD = Path(r"C:\Windows\Fonts\msyhbd.ttc")
FONT_MATH = Path(r"C:\Windows\Fonts\DejaVuMathTeXGyre.ttf")

NAVY = "1F4E79"
BLUE = "4472C4"
PALE_BLUE = "EAF2F8"
PALE_GRAY = "F4F6F7"
MID_GRAY = "D9E1F2"
LIGHT_BORDER = "D9D9D9"
TEXT = "1F1F1F"


TABLES = {
    "related": {
        "headers": ["方法类型", "主要优势", "超长程环境中的不足"],
        "rows": [
            ["单智能体", "状态集中、实现简单", "能力边界单一，长上下文易产生遗忘与漂移"],
            ["静态链式协作", "流程清晰、通信成本可控", "节点失效或需求变化后缺乏替代路径"],
            ["静态星型协作", "便于集中协调", "中心负载高，结构不能随任务阶段改变"],
            ["全连接或广播", "信息覆盖广", "通信近似平方增长，错误与冗余容易扩散"],
            ["学习式拓扑", "能够按任务分布优化结构", "通常缺少运行时硬约束、状态版本和恢复语义"],
            ["ZYRA", "动态、受约束、可恢复且可审计", "当前软评分仍依赖启发式配置，需要更大规模在线评估"],
        ],
        "widths": [1.15, 2.0, 3.15],
        "caption": "表 1 典型协作方式与 ZYRA 的研究位置",
    },
    "complexity": {
        "headers": ["阶段", "主要计算", "时间复杂度", "受控规模"],
        "rows": [
            ["语义候选构图", "角色、节点和边的有界联合搜索", "O(BKN + BKN log(BN))", "候选数、束宽和最大节点数"],
            ["环境条件修正", "边级遥测编码与残差计算", "O(E)", "候选边数"],
            ["贡献感知剪枝", "回执聚合与边效用排序", "O(R + E log E)", "回执数与候选边数"],
            ["约束投影", "权限、资源、端点与图不变量", "O(V + E + C)", "节点、边和约束数"],
            ["调度与接管", "Worker 过滤与稳定排序", "O(W log W)", "注册 Worker 数"],
        ],
        "widths": [1.25, 2.45, 1.65, 1.55],
        "caption": "表 2 核心运行时阶段的复杂度",
    },
    "implementation": {
        "headers": ["组成", "主要职责", "面向用户或系统的结果"],
        "rows": [
            ["CLI", "创建、控制、恢复和非交互执行任务", "长程任务默认入口"],
            ["Web 工作台", "观察拓扑、事件、权限、记忆、工件和验证", "可视化治理与证据查看"],
            ["API 服务", "托管任务、会话、事件和规范状态接口", "CLI 与 Web 的共同事实来源"],
            ["异构 Worker", "执行模型推理、工具调用和领域任务", "结构化交付、工件与局部回执"],
            ["状态与记忆", "事件、检查点、记忆视图和工件存储", "可重放历史与有限上下文"],
            ["验证与恢复", "完成谓词、连续性、租约和故障闭合", "可信终止或可恢复失败记录"],
        ],
        "widths": [1.3, 3.1, 2.1],
        "caption": "表 3 可部署系统的主要组成",
    },
    "sealed_results": {
        "headers": ["场景", "领域", "有效迁移", "故障注入", "人工干预", "无效迁移", "最终验证"],
        "rows": [
            ["software-sdk", "软件工程交付", "2,295", "5", "0", "0", "通过"],
            ["technical-intelligence", "跨源技术研究", "7,168", "5", "0", "0", "通过"],
        ],
        "widths": [1.35, 1.35, 0.85, 0.75, 0.75, 0.75, 0.75],
        "caption": "表 4 P2-S06-02 attempt 08 封存运行结果",
    },
    "metrics": {
        "headers": ["研究问题", "主要指标", "防止误判的配套指标"],
        "rows": [
            ["任务完成", "可验证完成率、义务覆盖率", "失败运行、验证器类型、样本量"],
            ["拓扑与通信", "通信字节、Token、边稀疏度", "证据利用率、拓扑抖动、关键路径覆盖"],
            ["记忆连续性", "关键事实召回、未决义务保持", "陈旧事实注入、来源完整性、重复工件"],
            ["故障恢复", "恢复率、恢复时延", "迟到提交、重复副作用、非法位置迁移"],
            ["资源效率", "墙钟时间、Token、费用", "任务成功、隐私和权限违规"],
        ],
        "widths": [1.4, 2.6, 2.6],
        "caption": "表 5 论文评测指标及其约束",
    },
}


FIGURE_CAPTIONS = {
    "dual_graph": "图 1 逻辑任务图与动态协作图的分离",
    "architecture": "图 2 ZYRA 中心化控制与去中心化执行架构",
    "topology": "图 3 动态异构协作图的在线更新流程",
    "communication": "图 4 义务定向的结构化通信与工件外置",
    "memory": "图 5 从事实账本到有限模型上下文的记忆连续性",
    "recovery": "图 6 Worker 失效后的围栏 接管与重新验证",
}


EQUATIONS = {
    "proposal": "p(t) ~ Pθ(· | Xobs(t), K(t)),     u(t) = Π[F(Xobs(t))](p(t))",
    "state": "X(t) = (D(t), G(t), O(t), M(t), H(t), B(t), Q(t), A(t))",
    "objective": (
        "max  ΔUobligation + λv·Uverify − λc·Ccomm − λl·Clatency\n"
        "u ∈ F(Xobs(t))          − λs·Cswitch − λf·Rfailure"
    ),
    "topology": "G(t,0)  →semantic  G(t,1)  →condition  G(t,2)  →prune  G(t,3)  →projection  G(t+1)",
    "arg": "sARG = wo·co + wa·ca + wd·cd + wk·ck + wr·cr − wm·cm − ws·cs",
    "card": "sCARD(e) = w^T φ(t,e) / Σj wj − κ(e),     H(t,e) = 0  ⇒  reject / drop",
    "prune": "min  Σe ke·Ce − λΣe ke·se,     s.t.  ke = 1 for every protected edge e",
    "compact": "(Ktilde(t), Aplus(t)) = C(K(t), Γ(t); P(t), Bctx(t)),     L'(t) = L(t)",
    "continuity": (
        "Gate(Cminus, Cplus) = 1  ⇔  Oopen(minus) ⊆ Oplus ∪ Osuperseded\n"
        "∧  every f ∈ Fcritical(plus) satisfies Safe(f)"
    ),
    "scheduling": (
        "w*(i) = arg max  s(i,j),     where χ(i,j) = 1\n"
        "             w(j): χ(i,j)=1              ⇒ capability, permission, privacy, capacity, health valid"
    ),
    "recovery": (
        "Recovered(anew) ⇔ LeaseValid(anew) ∧ ContinuityGate(anew)\n"
        "∧ ArtifactVerified(anew) ∧ PendingEffects(anew) = ∅"
    ),
    "success": (
        "Success(X(T)) ⇔ (∀ o(i) ∈ Orequired(T), p(i,A(T)) = 1) ∧ RequiredArtifacts\n"
        "∧ ContinuityGate ∧ NoOpenCriticalLease"
    ),
}


def font(path: Path, size: int):
    return ImageFont.truetype(str(path), size=size)


def draw_text_center(draw, box, text, font_obj, fill="#1F1F1F", spacing=10):
    x0, y0, x1, y1 = box
    bbox = draw.multiline_textbbox((0, 0), text, font=font_obj, spacing=spacing, align="center")
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.multiline_text(((x0 + x1 - tw) / 2, (y0 + y1 - th) / 2), text, font=font_obj, fill=fill, spacing=spacing, align="center")


def box(draw, xy, text, fill, outline="#4472C4", text_fill="#1F1F1F", size=34, radius=22):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=4)
    draw_text_center(draw, xy, text, font(FONT_CN_BOLD, size), fill=text_fill, spacing=8)


def arrow(draw, start, end, color="#4472C4", width=6):
    draw.line([start, end], fill=color, width=width)
    import math
    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    length = 18
    a1 = angle + math.pi * 0.84
    a2 = angle - math.pi * 0.84
    p1 = (end[0] + length * math.cos(a1), end[1] + length * math.sin(a1))
    p2 = (end[0] + length * math.cos(a2), end[1] + length * math.sin(a2))
    draw.polygon([end, p1, p2], fill=color)


def canvas():
    img = Image.new("RGB", (1800, 950), "white")
    return img, ImageDraw.Draw(img)


def make_dual_graph(path: Path):
    img, d = canvas()
    d.text((90, 52), "同一目标 两类图承担不同职责", font=font(FONT_CN_BOLD, 48), fill="#1F4E79")
    box(d, (100, 175, 760, 330), "逻辑任务图\n描述必须完成的义务与依赖", "#EAF2F8", size=38)
    box(d, (1040, 175, 1700, 330), "动态协作图\n描述当前执行者与信息方向", "#F3EAF8", outline="#7030A0", size=38)
    arrow(d, (760, 250), (1040, 250), color="#7F8C8D")
    d.text((805, 195), "义务分配", font=font(FONT_CN_REG, 31), fill="#555555")
    small = [
        ((125, 455, 335, 575), "需求理解", "#D9EAF7"),
        ((380, 455, 590, 575), "代码修改", "#D9EAF7"),
        ((635, 455, 845, 575), "验证交付", "#D9EAF7"),
        ((955, 455, 1165, 575), "规划 Worker", "#EDE3F2"),
        ((1210, 455, 1420, 575), "代码 Worker", "#EDE3F2"),
        ((1465, 455, 1675, 575), "验证 Worker", "#EDE3F2"),
    ]
    for xy, text, fill in small:
        box(d, xy, text, fill, size=28, radius=16)
    arrow(d, (335, 515), (380, 515), color="#4472C4")
    arrow(d, (590, 515), (635, 515), color="#4472C4")
    arrow(d, (1165, 515), (1210, 515), color="#7030A0")
    arrow(d, (1420, 515), (1465, 515), color="#7030A0")
    d.text((110, 690), "执行节点可以替换 业务义务与完成标准保持", font=font(FONT_CN_BOLD, 34), fill="#1F4E79")
    d.text((110, 758), "需求变化显式更新任务图 环境变化只调整协作图", font=font(FONT_CN_REG, 32), fill="#555555")
    img.save(path)


def make_architecture(path: Path):
    img, d = canvas()
    d.text((90, 40), "中心化状态控制 去中心化异构执行", font=font(FONT_CN_BOLD, 48), fill="#1F4E79")
    layers = [
        (120, 145, 1680, 255, "用户入口层  CLI  Web 工作台", "#EAF2F8", "#1F4E79"),
        (120, 300, 1680, 430, "控制平面  任务图  动态拓扑  权限预算  验证终止", "#D9EAF7", "#1F4E79"),
        (120, 475, 1680, 595, "执行平面  代码  浏览器  研究  模型  验证 Worker", "#EDE3F2", "#7030A0"),
        (120, 640, 1680, 750, "状态与记忆  事件日志  检查点  分层记忆  工件", "#F2F2F2", "#555555"),
        (120, 795, 1680, 895, "资源平面  终端  边缘  云端", "#E2F0D9", "#548235"),
    ]
    for x0, y0, x1, y1, text, fill, outline in layers:
        box(d, (x0, y0, x1, y1), text, fill, outline=outline, size=35, radius=18)
    for y in (255, 430, 595, 750):
        arrow(d, (900, y + 4), (900, y + 42), color="#7F8C8D", width=5)
    img.save(path)


def make_topology(path: Path):
    img, d = canvas()
    d.text((80, 55), "动态协作图只提交通过约束的状态增量", font=font(FONT_CN_BOLD, 46), fill="#1F4E79")
    labels = [
        ("语义候选构图", "任务需要谁", "#D9EAF7", "#4472C4"),
        ("环境条件修正", "当前谁可执行", "#E2F0D9", "#548235"),
        ("贡献感知剪枝", "哪些边值得保留", "#FCE4D6", "#C65911"),
        ("硬约束投影", "哪些变化合法", "#EDE3F2", "#7030A0"),
        ("版本化提交", "形成唯一生效图", "#F2F2F2", "#595959"),
    ]
    x = 70
    for idx, (title, sub, fill, outline) in enumerate(labels):
        xy = (x, 250, x + 290, 490)
        box(d, xy, f"{title}\n{sub}", fill, outline=outline, size=31, radius=20)
        if idx < len(labels) - 1:
            arrow(d, (x + 290, 370), (x + 340, 370), color="#7F8C8D")
        x += 340
    d.text((90, 650), "软评分不能覆盖权限 隐私 容量 预算 版本与图结构等硬条件", font=font(FONT_CN_BOLD, 33), fill="#1F4E79")
    d.text((90, 730), "同一快照产生候选 同一提交序列形成可重放的规范拓扑", font=font(FONT_CN_REG, 32), fill="#555555")
    img.save(path)


def make_communication(path: Path):
    img, d = canvas()
    d.text((80, 50), "传递结构化交付 不广播完整对话", font=font(FONT_CN_BOLD, 48), fill="#1F4E79")
    box(d, (90, 230, 430, 440), "上游 Worker\n结论 状态差量\n证据与工件", "#D9EAF7", size=34)
    box(d, (730, 190, 1070, 480), "中心控制平面\n义务匹配\n权限与预算\n定向路由", "#EDE3F2", outline="#7030A0", size=34)
    box(d, (1370, 230, 1710, 440), "下游 Worker\n按需读取\n提交消费证明", "#E2F0D9", outline="#548235", size=34)
    arrow(d, (430, 335), (730, 335))
    arrow(d, (1070, 335), (1370, 335))
    box(d, (560, 650, 1240, 815), "内容寻址工件库\n大型原文只存一次 消息传递稳定引用", "#F2F2F2", outline="#7F8C8D", size=33)
    arrow(d, (850, 480), (850, 650), color="#7F8C8D")
    arrow(d, (1390, 440), (1190, 650), color="#548235")
    img.save(path)


def make_memory(path: Path):
    img, d = canvas()
    d.text((70, 45), "事实账本与模型上下文分离", font=font(FONT_CN_BOLD, 48), fill="#1F4E79")
    box(d, (80, 190, 420, 350), "不可变事件日志\n内容寻址工件", "#F2F2F2", outline="#595959", size=34)
    arrow(d, (420, 270), (540, 270), color="#7F8C8D")
    memory_boxes = [
        ("工作记忆", 540, 145, "#D9EAF7", "#4472C4"),
        ("情景记忆", 880, 145, "#FCE4D6", "#C65911"),
        ("语义记忆", 540, 365, "#E2F0D9", "#548235"),
        ("技能记忆", 880, 365, "#EDE3F2", "#7030A0"),
    ]
    for label, x, y, fill, outline in memory_boxes:
        box(d, (x, y, x + 270, y + 150), label, fill, outline=outline, size=34)
    box(d, (1270, 190, 1710, 350), "检索 压缩\n连续性验证", "#FFF2CC", outline="#BF9000", size=35)
    arrow(d, (1150, 270), (1270, 270), color="#7F8C8D")
    box(d, (590, 680, 1210, 825), "有限模型上下文\n目标 约束 当前义务 关键事实 工件引用", "#EAF2F8", size=34)
    arrow(d, (1490, 350), (1080, 680), color="#BF9000")
    d.text((80, 560), "压缩改变模型本轮可见内容 不删除原始事件与证据", font=font(FONT_CN_BOLD, 32), fill="#1F4E79")
    img.save(path)


def make_recovery(path: Path):
    img, d = canvas()
    d.text((70, 50), "恢复不是简单重试 而是重新建立合法提交权", font=font(FONT_CN_BOLD, 45), fill="#1F4E79")
    y = 390
    d.line([(120, y), (1680, y)], fill="#A6A6A6", width=7)
    steps = [
        (170, "Worker A\n执行", "#D9EAF7", "#4472C4"),
        (460, "节点失效\n检测", "#F4CCCC", "#C00000"),
        (750, "围栏旧租约\n拒绝迟到结果", "#FCE4D6", "#C65911"),
        (1080, "Worker B\n从检查点接管", "#E2F0D9", "#548235"),
        (1430, "工件与状态\n重新验证", "#EDE3F2", "#7030A0"),
    ]
    for index, (x, label, fill, outline) in enumerate(steps):
        d.ellipse((x - 18, y - 18, x + 18, y + 18), fill=outline)
        above = index % 2 == 0
        box(d, (x - 125, 175 if above else 515, x + 125, 330 if above else 670), label, fill, outline=outline, size=28, radius=18)
        target_y = 330 if above else 515
        d.line([(x, y), (x, target_y)], fill=outline, width=4)
    d.text((110, 790), "能力 位置 隐私和未确认副作用约束在接管前后保持", font=font(FONT_CN_BOLD, 33), fill="#1F4E79")
    img.save(path)


def make_figures():
    ASSETS.mkdir(parents=True, exist_ok=True)
    makers = {
        "dual_graph": make_dual_graph,
        "architecture": make_architecture,
        "topology": make_topology,
        "communication": make_communication,
        "memory": make_memory,
        "recovery": make_recovery,
    }
    for name, maker in makers.items():
        maker(ASSETS / f"{name}.png")


def make_equations():
    equation_dir = ASSETS / "equations"
    equation_dir.mkdir(parents=True, exist_ok=True)
    math_font = font(FONT_MATH, 47)
    for name, formula in EQUATIONS.items():
        probe = Image.new("RGB", (20, 20), "white")
        probe_draw = ImageDraw.Draw(probe)
        bbox = probe_draw.multiline_textbbox((0, 0), formula, font=math_font, spacing=22, align="center")
        width = int(max(900, bbox[2] - bbox[0] + 120))
        height = int(bbox[3] - bbox[1] + 80)
        img = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(img)
        text_bbox = draw.multiline_textbbox((0, 0), formula, font=math_font, spacing=22, align="center")
        x = (width - (text_bbox[2] - text_bbox[0])) / 2
        y = (height - (text_bbox[3] - text_bbox[1])) / 2 - text_bbox[1]
        draw.multiline_text((x, y), formula, font=math_font, fill="#111111", spacing=22, align="center")
        img.save(equation_dir / f"{name}.png", dpi=(300, 300))


def set_run_font(run, cn=FONT_BODY, latin=FONT_LATIN, size=None, bold=None, color=TEXT):
    run.font.name = latin
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    run.font.color.rgb = RGBColor.from_string(color)
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.rFonts
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), latin)
    rfonts.set(qn("w:hAnsi"), latin)
    rfonts.set(qn("w:eastAsia"), cn)


def configure_styles(doc: Document):
    styles = doc.styles
    normal = styles["Normal"]
    normal.font.name = FONT_LATIN
    normal.font.size = Pt(11)
    normal.font.color.rgb = RGBColor.from_string(TEXT)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), FONT_BODY)
    normal.paragraph_format.line_spacing = 1.45
    normal.paragraph_format.space_after = Pt(5)
    normal.paragraph_format.first_line_indent = Pt(22)
    normal.paragraph_format.widow_control = True

    title = styles["Title"]
    title.font.name = FONT_LATIN
    title.font.size = Pt(24)
    title.font.bold = True
    title.font.color.rgb = RGBColor(0, 0, 0)
    title._element.rPr.rFonts.set(qn("w:eastAsia"), FONT_HEAD)
    title.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_after = Pt(18)

    for name, size, before, after in [("Heading 1", 16, 18, 10), ("Heading 2", 13, 14, 7), ("Heading 3", 11.5, 10, 5)]:
        style = styles[name]
        style.font.name = FONT_LATIN
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor(0, 0, 0)
        style._element.rPr.rFonts.set(qn("w:eastAsia"), FONT_HEAD)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.page_break_before = False

    if "Caption" in styles:
        cap = styles["Caption"]
    else:
        cap = styles.add_style("Caption", WD_STYLE_TYPE.PARAGRAPH)
    cap.font.name = FONT_LATIN
    cap.font.size = Pt(9.5)
    cap.font.color.rgb = RGBColor.from_string(TEXT)
    cap._element.rPr.rFonts.set(qn("w:eastAsia"), FONT_BODY)
    cap.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap.paragraph_format.space_before = Pt(4)
    cap.paragraph_format.space_after = Pt(9)
    cap.paragraph_format.keep_with_next = False

    alg = styles.add_style("Algorithm", WD_STYLE_TYPE.PARAGRAPH)
    alg.font.name = FONT_MONO
    alg.font.size = Pt(9.5)
    alg._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    alg.paragraph_format.left_indent = Cm(0.8)
    alg.paragraph_format.first_line_indent = Cm(-0.3)
    alg.paragraph_format.line_spacing = 1.15
    alg.paragraph_format.space_after = Pt(2)
    alg.paragraph_format.keep_together = True

    ref = styles.add_style("Reference", WD_STYLE_TYPE.PARAGRAPH)
    ref.font.name = FONT_LATIN
    ref.font.size = Pt(9.5)
    ref._element.rPr.rFonts.set(qn("w:eastAsia"), FONT_BODY)
    ref.paragraph_format.left_indent = Cm(0.75)
    ref.paragraph_format.first_line_indent = Cm(-0.75)
    ref.paragraph_format.line_spacing = 1.2
    ref.paragraph_format.space_after = Pt(3)


def set_repeat_table_header(row):
    tr_pr = row._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)


def set_cell_shading(cell, fill):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_cell_margins(cell, top=90, start=110, bottom=90, end=110):
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for m, v in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{m}"))
        if node is None:
            node = OxmlElement(f"w:{m}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(v))
        node.set(qn("w:type"), "dxa")


def set_table_borders(table, color=LIGHT_BORDER, size=6):
    tbl_pr = table._tbl.tblPr
    borders = tbl_pr.first_child_found_in("w:tblBorders")
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        tbl_pr.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        elem = borders.find(qn(f"w:{edge}"))
        if elem is None:
            elem = OxmlElement(f"w:{edge}")
            borders.append(elem)
        elem.set(qn("w:val"), "single")
        elem.set(qn("w:sz"), str(size))
        elem.set(qn("w:color"), color)


def set_cell_width(cell, inches):
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_w = tc_pr.find(qn("w:tcW"))
    if tc_w is None:
        tc_w = OxmlElement("w:tcW")
        tc_pr.append(tc_w)
    tc_w.set(qn("w:w"), str(int(inches * 1440)))
    tc_w.set(qn("w:type"), "dxa")


def add_table(doc, spec):
    p = doc.add_paragraph(spec["caption"], style="Caption")
    p.paragraph_format.keep_with_next = True
    table = doc.add_table(rows=1, cols=len(spec["headers"]))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    set_table_borders(table)
    header = table.rows[0]
    set_repeat_table_header(header)
    for idx, text in enumerate(spec["headers"]):
        cell = header.cells[idx]
        set_cell_width(cell, spec["widths"][idx])
        set_cell_shading(cell, NAVY)
        set_cell_margins(cell)
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.first_line_indent = None
        p.paragraph_format.space_after = Pt(0)
        run = p.add_run(text)
        set_run_font(run, cn=FONT_HEAD, size=9.2, bold=True, color="FFFFFF")
    for ridx, row_data in enumerate(spec["rows"]):
        row = table.add_row()
        for idx, text in enumerate(row_data):
            cell = row.cells[idx]
            set_cell_width(cell, spec["widths"][idx])
            set_cell_shading(cell, "FFFFFF" if ridx % 2 == 0 else PALE_BLUE)
            set_cell_margins(cell)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER if idx == 0 or len(spec["headers"]) > 4 else WD_ALIGN_PARAGRAPH.LEFT
            p.paragraph_format.first_line_indent = None
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.line_spacing = 1.15
            run = p.add_run(str(text))
            set_run_font(run, size=9.0)
    doc.add_paragraph().paragraph_format.space_after = Pt(0)


def add_field(paragraph, instruction):
    run = paragraph.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = instruction
    fld_sep = OxmlElement("w:fldChar")
    fld_sep.set(qn("w:fldCharType"), "separate")
    placeholder = OxmlElement("w:t")
    placeholder.text = ""
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_begin)
    run._r.append(instr)
    run._r.append(fld_sep)
    run._r.append(placeholder)
    run._r.append(fld_end)
    return run


def add_toc(doc):
    p = doc.add_paragraph()
    p.style = doc.styles["Title"]
    p.add_run("目录")
    toc = doc.add_paragraph()
    toc.paragraph_format.first_line_indent = None
    add_field(toc, 'TOC \\o "1-3" \\h \\z \\u')
    settings = doc.settings._element
    update = settings.find(qn("w:updateFields"))
    if update is None:
        update = OxmlElement("w:updateFields")
        settings.append(update)
    update.set(qn("w:val"), "true")


def add_page_number(section):
    section.different_first_page_header_footer = True
    footer = section.footer
    p = footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = None
    run = add_field(p, "PAGE")
    set_run_font(run, size=9, color="666666")


def set_picture_alt(run, description):
    drawing = run._element.xpath(".//wp:docPr")
    if drawing:
        drawing[0].set("descr", description)


def add_figure(doc, name):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = None
    p.paragraph_format.keep_with_next = True
    run = p.add_run()
    run.add_picture(str(ASSETS / f"{name}.png"), width=Inches(6.25))
    set_picture_alt(run, FIGURE_CAPTIONS[name])
    doc.add_paragraph(FIGURE_CAPTIONS[name], style="Caption")


def add_equation(doc, name, number):
    path = ASSETS / "equations" / f"{name}.png"
    with Image.open(path) as im:
        width = min(6.0, max(2.6, im.width / 260.0))
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = None
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(6)
    run = p.add_run()
    run.add_picture(str(path), width=Inches(width))
    set_picture_alt(run, f"公式 {number}")
    nr = p.add_run(f"    （{number}）")
    set_run_font(nr, size=10)


def add_cover(doc):
    for _ in range(3):
        doc.add_paragraph()
    p = doc.add_paragraph(style="Title")
    p.paragraph_format.space_after = Pt(16)
    run = p.add_run("ZYRA 面向超长程复杂任务的\n动态异构多智能体协同系统")
    set_run_font(run, cn=FONT_HEAD, size=24, bold=True, color="000000")
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = None
    p.paragraph_format.space_after = Pt(70)
    run = p.add_run("参赛技术论文")
    set_run_font(run, cn=FONT_HEAD, size=15, bold=True, color=NAVY)
    info = [
        ("项目名称", "ZYRA 智衍群策"),
        ("赛题编号", "XH-202631"),
        ("赛题名称", "面向超长程复杂任务的动态异构群体智能架构\n与深度协同推理技术"),
    ]
    table = doc.add_table(rows=len(info), cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    for r, (label, value) in enumerate(info):
        for c, text in enumerate((label, value)):
            cell = table.cell(r, c)
            set_cell_width(cell, 1.25 if c == 0 else 4.65)
            set_cell_margins(cell, top=130, bottom=130)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            p = cell.paragraphs[0]
            p.paragraph_format.first_line_indent = None
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER if c == 0 else WD_ALIGN_PARAGRAPH.LEFT
            run = p.add_run(text)
            set_run_font(run, cn=FONT_HEAD if c == 0 else FONT_BODY, size=11, bold=c == 0)
    set_table_borders(table, color="FFFFFF", size=0)
    for _ in range(4):
        doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = None
    run = p.add_run("2026 年 9 月")
    set_run_font(run, cn=FONT_HEAD, size=12, bold=True)


def is_reference(text):
    return bool(re.match(r"^\[\d+\]", text))


def add_body_paragraph(doc, text, in_references=False):
    style = "Reference" if in_references and is_reference(text) else "Normal"
    p = doc.add_paragraph(style=style)
    if text.startswith("关键词："):
        p.paragraph_format.first_line_indent = None
        label, rest = text.split("：", 1)
        r1 = p.add_run(label + "：")
        set_run_font(r1, cn=FONT_HEAD, size=11, bold=True)
        r2 = p.add_run(rest)
        set_run_font(r2, size=11)
        return
    run = p.add_run(text)
    set_run_font(run, size=11 if style == "Normal" else 9.5)


def build():
    ASSETS.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    make_figures()
    make_equations()

    doc = Document()
    configure_styles(doc)
    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.35)
    section.bottom_margin = Cm(2.25)
    section.left_margin = Cm(2.55)
    section.right_margin = Cm(2.55)
    add_page_number(section)
    doc.core_properties.title = "ZYRA 面向超长程复杂任务的动态异构多智能体协同系统"
    doc.core_properties.subject = "挑战杯参赛技术论文"
    doc.core_properties.keywords = "ZYRA, 多智能体, 动态拓扑, 长程记忆, 故障恢复"
    doc.core_properties.author = "ZYRA 项目团队"

    add_cover(doc)
    doc.add_page_break()

    text = SOURCE.read_text(encoding="utf-8")
    first_break = text.find("[PAGEBREAK]")
    if first_break < 0:
        raise RuntimeError("source has no title-page boundary")
    lines = text[first_break + len("[PAGEBREAK]"):].splitlines()
    equation_number = 0
    in_algorithm = False
    in_references = False
    skip_blank_after_break = False
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        i += 1
        if not line:
            continue
        if line == "[PAGEBREAK]":
            doc.add_page_break()
            skip_blank_after_break = True
            continue
        if line == "[TOC]":
            add_toc(doc)
            continue
        m = re.match(r"\[FIGURE:([a-z_]+)\]", line)
        if m:
            add_figure(doc, m.group(1))
            continue
        m = re.match(r"\[EQUATION:([a-z_]+)\]", line)
        if m:
            equation_number += 1
            add_equation(doc, m.group(1), equation_number)
            continue
        m = re.match(r"\[TABLE:([a-z_]+)\]", line)
        if m:
            add_table(doc, TABLES[m.group(1)])
            continue
        m = re.match(r"\[ALGORITHM:(.+)\]", line)
        if m:
            cap = doc.add_paragraph(m.group(1), style="Caption")
            cap.paragraph_format.keep_with_next = True
            in_algorithm = True
            continue
        if line == "[ENDALGORITHM]":
            in_algorithm = False
            doc.add_paragraph().paragraph_format.space_after = Pt(0)
            continue
        if in_algorithm:
            p = doc.add_paragraph(style="Algorithm")
            run = p.add_run(line)
            set_run_font(run, cn="Microsoft YaHei", latin=FONT_MONO, size=9.5)
            continue
        if line.startswith("# "):
            heading = line[2:].strip()
            in_references = heading == "参考文献"
            p = doc.add_paragraph(style="Title" if heading in {"摘要", "目录"} else "Heading 1")
            p.paragraph_format.first_line_indent = None
            run = p.add_run(heading)
            set_run_font(run, cn=FONT_HEAD, size=16 if heading not in {"摘要", "目录"} else 20, bold=True, color="000000")
            continue
        if line.startswith("## "):
            p = doc.add_paragraph(style="Heading 2")
            p.paragraph_format.first_line_indent = None
            run = p.add_run(line[3:].strip())
            set_run_font(run, cn=FONT_HEAD, size=13, bold=True, color="000000")
            continue
        if line.startswith("### "):
            p = doc.add_paragraph(style="Heading 3")
            p.paragraph_format.first_line_indent = None
            run = p.add_run(line[4:].strip())
            set_run_font(run, cn=FONT_HEAD, size=11.5, bold=True, color="000000")
            continue
        add_body_paragraph(doc, line, in_references=in_references)

    doc.save(DOCX_PATH)
    print(DOCX_PATH)


if __name__ == "__main__":
    try:
        build()
    except Exception as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        raise
