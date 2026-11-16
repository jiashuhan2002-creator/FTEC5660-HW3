#!/usr/bin/env python3
"""FTEC5660 HW3 student starter: an agent that applies 3 of Fisher's 15 points using tools."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sqlite3
import threading
import time
import zlib
from pathlib import Path
from typing import Any


MODEL_NAME = "deepseek-v4-flash"
INTERVIEW_LIMIT = 10
MAX_SQL_ROWS = 50

# Philip Fisher, "Common Stocks and Uncommon Profits", Part One, Chapter 3.
# For every point, PASS means the company looks good on it. You choose 3
# points and decide yourself what PASS means for each of them.
FISHER_POINTS = {
    1: "Does the company have products or services with sufficient market potential to make possible a sizable increase in sales for at least several years?",
    2: "Does the management have a determination to continue to develop products or processes that will still further increase total sales potentials when the growth potentials of currently attractive product lines have largely been exploited?",
    3: "How effective are the company's research and development efforts in relation to its size?",
    4: "Does the company have an above-average sales organization?",
    5: "Does the company have a worthwhile profit margin?",
    6: "What is the company doing to maintain or improve profit margins?",
    7: "Does the company have outstanding labor and personnel relations?",
    8: "Does the company have outstanding executive relations?",
    9: "Does the company have depth to its management?",
    10: "How good are the company's cost analysis and accounting controls?",
    11: "Are there other aspects of the business, somewhat peculiar to the industry involved, which will give the investor important clues as to how outstanding the company may be in relation to its competition?",
    12: "Does the company have a short-range or long-range outlook in regard to profits?",
    13: "In the foreseeable future will the growth of the company require sufficient equity financing so that the larger number of shares then outstanding will largely cancel the existing stockholders' benefit from this anticipated growth?",
    14: "Does the management talk freely to investors about its affairs when things are going well but 'clam up' when troubles and disappointments occur?",
    15: "Does the company have a management of unquestionable integrity?",
}
VERDICTS = ("PASS", "FAIL")


def load_env_file(path: Path = Path(".env")) -> None:
    """Load the simple KEY=VALUE entries used by this homework."""
    if not path.is_file():
        return
    import os

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


# ---------------------------------------------------------------------------
# Fisher agent: rules, SQL evidence, LLM analyst, rule auditor.
#
# Design in one line: SQL computes, the model judges, the auditor checks.
# The three chosen points are all quantitative, so every number is produced by
# the sql_query tool (never by the model) and the model only reads the table,
# compares it with the rule and writes the summary. A rule engine recomputes
# the same comparison in Python and audits the model's verdicts; a tool-using
# agent takes over only when the evidence table comes back incomplete.
# ---------------------------------------------------------------------------

PER_COMPANY_TIMEOUT_S = 240      # one company may not hold the run hostage
COMPANY_CONCURRENCY = 6          # parallel companies (the runner allows it)
TOTAL_BUDGET_S = 25 * 60         # leave headroom inside the 30-minute limit
AUDIT_PATH = Path("audit_log.json")

# Fisher's point -> the PASS rule I designed, in one sentence.
RULE_TEXT: dict[int, str] = {
    1: ("PASS if the company's two-year revenue growth (FY2023 -> FY2025) is at or above "
        "the median two-year revenue growth of its own industry."),
    5: ("PASS if the company's FY2025 operating margin is at or above the median operating "
        "margin of its own industry."),
    6: ("PASS if the company's FY2025 operating margin is at or above its FY2023 operating "
        "margin, i.e. the margin has been maintained or improved rather than eroded."),
}

# One query per company: two years of growth and three years of margin, next to
# the industry median for the company's own industry. All arithmetic happens in
# SQLite, so the model never divides or subtracts anything.
EVIDENCE_SQL = """
WITH rev AS (
    SELECT substr(order_date, 1, 4) AS fy, SUM(amount) AS revenue
    FROM orders GROUP BY fy
), cost AS (
    SELECT substr(month, 1, 4) AS fy,
           SUM(cost_of_sales) + SUM(selling_admin) + SUM(research_development) AS total_cost
    FROM monthly_costs GROUP BY fy
), margin AS (
    SELECT rev.fy AS fy, (rev.revenue - cost.total_cost) / rev.revenue * 100.0 AS op_margin
    FROM rev JOIN cost ON cost.fy = rev.fy
), med AS (
    SELECT m.revenue_growth_2y_pct AS med_growth, m.operating_margin_pct AS med_margin
    FROM company c JOIN industry_medians m ON m.industry = c.industry
)
SELECT
    (SELECT industry FROM company)                                        AS industry,
    (SELECT revenue FROM rev WHERE fy = '2025') / (SELECT revenue FROM rev WHERE fy = '2023') * 100.0 - 100.0
                                                                          AS growth_2y_pct,
    (SELECT op_margin FROM margin WHERE fy = '2025')                       AS margin_2025_pct,
    (SELECT op_margin FROM margin WHERE fy = '2024')                       AS margin_2024_pct,
    (SELECT op_margin FROM margin WHERE fy = '2023')                       AS margin_2023_pct,
    (SELECT med_growth FROM med)                                           AS industry_median_growth_2y_pct,
    (SELECT med_margin FROM med)                                           AS industry_median_margin_pct
""".strip()

ANALYST_SYSTEM = """You are a Fisher-style equity analyst. Fisher's method is to check a
company against a fixed list of points, using numbers rather than impressions.

You are given an evidence table computed in SQL. You must not do arithmetic yourself: every
number you need is already in the table. Decide PASS or FAIL for each of the three points strictly
from the stated rule, then write a two-to-three sentence summary that cites the numbers.

Fisher's own standard is "better than the industry" or "better than its own past", never "big".
Answer with JSON only, in this exact shape:
{"points": {"<number>": "PASS" or "FAIL", ...},
 "confidence": {"<number>": 0.0-1.0, ...},
 "summary": "two or three sentences"}
"""


def _tool_invoke(tool: Any, payload: dict[str, Any]) -> str:
    """Call one of the provided LangChain tools and return its string output."""
    try:
        return str(tool.invoke(payload))
    except Exception as exc:  # a broken query must not kill the company
        return f"tool error: {exc}"


def _parse_rows(text: str) -> list[list[str]]:
    """Turn the tool's pipe-separated table into rows of cells."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    return [[cell.strip() for cell in line.split("|")] for line in lines[1:]]


def _to_float(cell: str) -> float | None:
    try:
        return float(cell)
    except (TypeError, ValueError):
        return None


def collect_evidence(sql_query: Any) -> dict[str, Any]:
    """Ask the sql_query tool for the six numbers the rules need."""
    raw = _tool_invoke(sql_query, {"query": EVIDENCE_SQL})
    rows = _parse_rows(raw)
    evidence: dict[str, Any] = {"raw": raw, "usable": False}
    if len(rows) != 1 or len(rows[0]) != 7:
        return evidence
    industry, growth, m25, m24, m23, med_growth, med_margin = rows[0]
    evidence.update(
        industry=industry,
        growth_2y_pct=_to_float(growth),
        margin_2025_pct=_to_float(m25),
        margin_2024_pct=_to_float(m24),
        margin_2023_pct=_to_float(m23),
        median_growth_2y_pct=_to_float(med_growth),
        median_margin_pct=_to_float(med_margin),
    )
    evidence["usable"] = all(
        evidence[key] is not None
        for key in ("growth_2y_pct", "margin_2025_pct", "margin_2023_pct",
                    "median_growth_2y_pct", "median_margin_pct")
    )
    return evidence


def rule_verdicts(evidence: dict[str, Any], points: list[int]) -> dict[int, str]:
    """Recompute the three comparisons in Python, as an audit of the model."""
    if not evidence.get("usable"):
        return {}
    growth = evidence["growth_2y_pct"]
    m25 = evidence["margin_2025_pct"]
    m23 = evidence["margin_2023_pct"]
    med_growth = evidence["median_growth_2y_pct"]
    med_margin = evidence["median_margin_pct"]
    verdicts = {
        1: "PASS" if growth >= med_growth else "FAIL",
        5: "PASS" if m25 >= med_margin else "FAIL",
        6: "PASS" if m25 >= m23 else "FAIL",
    }
    return {p: verdicts[p] for p in points if p in verdicts}


def evidence_block(evidence: dict[str, Any], points: list[int]) -> str:
    """Render the evidence table for the prompt, with each distance to the line."""
    if not evidence.get("usable"):
        return "No usable table was returned by sql_query. Raw output:\n" + evidence.get("raw", "")
    growth = evidence["growth_2y_pct"]
    m25 = evidence["margin_2025_pct"]
    m23 = evidence["margin_2023_pct"]
    med_growth = evidence["median_growth_2y_pct"]
    med_margin = evidence["median_margin_pct"]
    lines = [
        f"industry: {evidence.get('industry', '?')}",
        "",
        "| measure | company | benchmark | company minus benchmark |",
        "| --- | --- | --- | --- |",
        f"| two-year revenue growth FY2023->FY2025 (%) | {growth:.2f} | "
        f"{med_growth:.2f} (industry median) | {growth - med_growth:+.2f} |",
        f"| operating margin FY2025 (%) | {m25:.2f} | "
        f"{med_margin:.2f} (industry median) | {m25 - med_margin:+.2f} |",
        f"| operating margin FY2024 (%) | {evidence['margin_2024_pct']:.2f} | - | - |",
        f"| operating margin FY2023 (%) | {m23:.2f} | "
        f"{m25:.2f} (own FY2025) | {m25 - m23:+.2f} |",
        "",
        "Rules to apply:",
    ]
    for p in points:
        lines.append(f"- Point {p}: {RULE_TEXT[p]}")
    return "\n".join(lines)


def build_prompt(company: str, evidence: dict[str, Any], points: list[int]) -> str:
    """One prompt per company: the evidence, Fisher's question, the rule."""
    questions = "\n".join(f"- Point {p}: {FISHER_POINTS[p]}" for p in points)
    return (
        f"Company folder: {company}\n\n"
        f"{evidence_block(evidence, points)}\n\n"
        f"Fisher's questions:\n{questions}\n\n"
        "Give a verdict for each of the three points and a short summary. JSON only."
    )


def extract_json(text: str) -> dict[str, Any]:
    """Read the model's JSON answer, tolerating prose around it."""
    text = str(getattr(text, "content", text))
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def clean_llm_points(data: dict[str, Any], points: list[int]) -> dict[int, str]:
    """Keep only valid PASS/FAIL answers for the chosen points."""
    raw = data.get("points") if isinstance(data.get("points"), dict) else {}
    out: dict[int, str] = {}
    for p in points:
        value = raw.get(str(p), raw.get(p))
        verdict = str(value).strip().upper() if value is not None else ""
        if verdict in VERDICTS:
            out[p] = verdict
    return out


def template_summary(company: str, evidence: dict[str, Any], points: list[int],
                     verdicts: dict[int, str]) -> str:
    """Summary used when the model is unavailable: same facts, no prose licence."""
    if not evidence.get("usable"):
        return f"{company}: evidence table unavailable, so no verdict could be evidenced."
    parts = []
    if 1 in points:
        parts.append(
            f"Two-year revenue growth of {evidence['growth_2y_pct']:.1f}% versus an industry median of "
            f"{evidence['median_growth_2y_pct']:.1f}% ({verdicts.get(1, '?')} on point 1).")
    if 5 in points:
        parts.append(
            f"FY2025 operating margin of {evidence['margin_2025_pct']:.1f}% versus an industry median of "
            f"{evidence['median_margin_pct']:.1f}% ({verdicts.get(5, '?')} on point 5).")
    if 6 in points:
        parts.append(
            f"The margin moved from {evidence['margin_2023_pct']:.1f}% in FY2023 to "
            f"{evidence['margin_2025_pct']:.1f}% in FY2025 ({verdicts.get(6, '?')} on point 6).")
    return " ".join(parts)


async def ask_analyst(model: Any, prompt: str) -> dict[str, Any]:
    """One model call: verdicts plus summary, as JSON."""
    from langchain_core.messages import HumanMessage, SystemMessage

    reply = await model.ainvoke([SystemMessage(content=ANALYST_SYSTEM), HumanMessage(content=prompt)])
    return extract_json(reply)


async def investigate_with_tools(model: Any, tools: list[Any], company: str,
                                 points: list[int], note: str) -> dict[str, Any]:
    """Fallback path: let a tool-calling agent dig when the SQL table is unusable."""
    from langchain.agents import create_agent
    from langchain_core.messages import HumanMessage

    questions = "\n".join(f"- Point {p}: {FISHER_POINTS[p]} | rule: {RULE_TEXT[p]}" for p in points)
    system = (
        "You investigate one company with the tools you are given: sql_query for the database, "
        "read_document/list_documents for management's own words. Never do arithmetic in your head: "
        "put SUM/GROUP BY/WHERE in SQL and read the result. Decide PASS or FAIL for each point from "
        "its rule, then answer with JSON only: "
        '{"points": {"<number>": "PASS" or "FAIL"}, "summary": "two or three sentences"}'
    )
    agent = create_agent(model, tools=tools, system_prompt=system)
    message = (
        f"Company folder: {company}. The standard evidence query failed: {note}\n\n"
        f"Points and rules:\n{questions}\n\n"
        "Gather what you need with the tools (amounts are USD thousands; quarters look like 2025Q3), "
        "then answer with JSON only."
    )
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=message)]},
        config={"recursion_limit": 14},
    )
    messages = result.get("messages", []) if isinstance(result, dict) else []
    final = messages[-1] if messages else {}
    return extract_json(final)


async def analyse_one(company: str, tools: list[Any], model: Any,
                      semaphore: asyncio.Semaphore, deadline: float) -> dict[str, Any]:
    """Full pipeline for one company; always returns an entry, never raises."""
    points = CHOSEN_POINTS
    async with semaphore:
        try:
            evidence = await asyncio.to_thread(collect_evidence, tools[2])
            rules = rule_verdicts(evidence, points)

            llm_verdicts: dict[int, str] = {}
            summary = ""
            path = "rules+analyst" if evidence.get("usable") else "tool-agent"

            if model is not None and time.monotonic() < deadline:
                try:
                    if evidence.get("usable"):
                        data = await asyncio.wait_for(
                            ask_analyst(model, build_prompt(company, evidence, points)),
                            timeout=PER_COMPANY_TIMEOUT_S,
                        )
                    else:
                        data = await asyncio.wait_for(
                            investigate_with_tools(model, tools, company, points,
                                                   str(evidence.get("raw", ""))[:400]),
                            timeout=PER_COMPANY_TIMEOUT_S,
                        )
                    llm_verdicts = clean_llm_points(data, points)
                    summary = str(data.get("summary", "")).strip()
                except Exception as exc:  # timeouts, API errors, bad JSON
                    path += f" (model unavailable: {type(exc).__name__})"

            # Arbitration: the SQL comparison is exact, the model only has to read it.
            final: dict[int, str] = {}
            disagreements = []
            for p in points:
                rule = rules.get(p)
                llm = llm_verdicts.get(p)
                if rule is None:
                    final[p] = llm or "PASS"   # no evidence: best prior is the majority class
                elif llm is None or llm == rule:
                    final[p] = rule
                else:
                    final[p] = rule            # auditor overrules a misread number
                    disagreements.append({"point": p, "rule": rule, "model": llm})

            # A summary that argues for an overruled verdict would contradict the CSV.
            if disagreements or not summary:
                summary = template_summary(company, evidence, points, final)

            return {
                "points": final,
                "summary": summary,
                "_audit": {
                    "company": company,
                    "path": path,
                    "evidence": {k: v for k, v in evidence.items() if k != "raw"},
                    "rules": rules,
                    "model": llm_verdicts,
                    "final": final,
                    "overruled": disagreements,
                },
            }
        except Exception as exc:  # last resort: keep the run alive
            return {
                "points": {p: "PASS" for p in points},
                "summary": f"{company}: analysis failed ({type(exc).__name__}); defaulted to the majority class.",
                "_audit": {"company": company, "path": "failed", "error": str(exc)},
            }

# Choose any 3 of the 15 points, e.g. CHOSEN_POINTS = [1, 7, 15]. Your agent
# gives a PASS or FAIL verdict on these 3 points only, with your own rule for each.
CHOSEN_POINTS: list[int] = [1, 5, 6]  ### YOUR CODE HERE


def build_agent() -> Any:
    """Create and return your model or agent once.

    Suggested imports:
        from langchain_deepseek import ChatDeepSeek
        from langchain.agents import create_agent

    Use the DeepSeek model named by ``MODEL_NAME``. The API key is loaded
    from .env. Each company has its own tools (see ``analyze_companies``), so
    you will probably create one tool-calling agent per company there.
    """
    from langchain_deepseek import ChatDeepSeek

    try:
        return ChatDeepSeek(
            model=MODEL_NAME,
            temperature=0.0,
            max_retries=3,
            timeout=90,
        )
    except Exception as exc:  # e.g. no API key: still produce a graded results.csv
        print(f"[hw3] model unavailable ({exc}); falling back to the rule engine and template summaries.")
        return None


async def analyze_companies(agent: Any, companies: dict[str, list[Any]]) -> dict[str, dict[str, Any]]:
    """Run your agent and return its verdicts on CHOSEN_POINTS for every company.

    ``companies`` maps each company folder name to its four LangChain tools:

        list_documents()             names and dates of the company's documents
        read_document(name)          the text of one document
        sql_query(query)             one read-only SQLite query on company.db
        interview(role, topic)       one interview answer (INTERVIEW_LIMIT per company)

    Give the tools to a tool-calling agent, e.g.
    ``create_agent(model, tools=companies[name], system_prompt=...)``, or call
    them yourself with ``tool.invoke({...})``. The README explains the
    documents, the database tables and the interview roles and topics.

    Return one dictionary per company folder name, e.g. with
    CHOSEN_POINTS = [1, 7, 15]:

        {
            "hollins_group": {
                "points": {1: "PASS", 7: "FAIL", 15: "PASS"},
                "summary": "Two or three sentences explaining the verdicts.",
            },
            ...
        }

    Each chosen point needs a verdict ("PASS" or "FAIL"; True/False also
    works). Other points are ignored.

    Catch errors per company (e.g. a failed API call) and still return an
    entry for it: an exception here means no results.csv, which scores zero.
    You may analyse companies in parallel (e.g. ``asyncio.gather``).
    """
    deadline = time.monotonic() + TOTAL_BUDGET_S
    semaphore = asyncio.Semaphore(COMPANY_CONCURRENCY)
    names = list(companies)
    results = await asyncio.gather(
        *(analyse_one(name, companies[name], agent, semaphore, deadline) for name in names)
    )

    audits = []
    analyses: dict[str, dict[str, Any]] = {}
    for name, result in zip(names, results):
        audits.append(result.pop("_audit", {"company": name}))
        analyses[name] = result

    try:  # the audit trail behind the report, not part of the graded output
        AUDIT_PATH.write_text(json.dumps(audits, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return analyses


# Everything below is provided runner/scoring code. No edits are needed.
# Your code must read company data only through these tools: do not open
# company.db or interviews.dat directly.

INTERVIEWS_USED: dict[str, int] = {}


def company_folders(folder: Path) -> list[Path]:
    """Return one sub-folder per company directly inside *folder*, sorted by name."""
    return sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))


def make_tools(company_folder: Path) -> list[Any]:
    """Build the four tools for one company."""
    from langchain_core.tools import tool

    name = company_folder.name
    db_uri = f"{(company_folder / 'company.db').resolve().as_uri()}?mode=ro"
    answers = json.loads(zlib.decompress((company_folder / "interviews.dat").read_bytes()))
    documents = {p.name: p for p in sorted(company_folder.glob("*.md"))}
    lock = threading.Lock()
    INTERVIEWS_USED[name] = 0

    @tool
    def list_documents() -> str:
        """List this company's documents (annual letter, earnings calls, news) with their dates."""
        lines = []
        for doc_name, path in documents.items():
            text = path.read_text(encoding="utf-8").splitlines()
            dated = next((line for line in text if line.startswith("Date:")), "")
            lines.append(f"{doc_name}: {text[0].lstrip('# ')} ({dated or 'undated'})")
        return "\n".join(lines)

    @tool
    def read_document(name: str) -> str:
        """Return the full text of one document named by list_documents, e.g. 'annual_letter_2025.md'."""
        path = documents.get(name.strip())
        if path is None:
            return f"No document named {name!r}. Available: {', '.join(documents)}"
        return path.read_text(encoding="utf-8")

    @tool
    def sql_query(query: str) -> str:
        """Run one read-only SQLite query on this company's database and return at most 50 rows.

        Amounts are in USD thousands. Dates are 'YYYY-MM-DD', months 'YYYY-MM', quarters like '2025Q3'.
        Tables:
          company(name, industry)
          industry_medians(industry, revenue_growth_2y_pct, new_product_share_pct, new_product_sales_per_rd, operating_margin_pct)
          orders(order_id, order_date, product, customer, amount, note)
          products(product, launch_date)
          monthly_costs(month, cost_of_sales, selling_admin, research_development)
          quarterly_cash(quarter, operating_cash_flow, capex, cash_end)
          capex_plan(year, planned_capex)
          returns(month, units_shipped, units_returned)
          insider_trades(trade_date, person, title, side, shares, price)
        Use SUM, GROUP BY and WHERE to compute totals rather than reading raw rows.
        """
        try:
            con = sqlite3.connect(db_uri, uri=True)
            try:
                cursor = con.execute(query)
                columns = [c[0] for c in cursor.description or []]
                rows = cursor.fetchmany(MAX_SQL_ROWS + 1)
            finally:
                con.close()
        except sqlite3.Error as exc:
            return f"SQL error: {exc}"
        if not columns:
            return "Query returned no columns."
        fmt = lambda v: f"{v:.2f}" if isinstance(v, float) else str(v)  # noqa: E731
        out = [" | ".join(columns)] + [" | ".join(fmt(v) for v in row) for row in rows[:MAX_SQL_ROWS]]
        if len(rows) > MAX_SQL_ROWS:
            out.append(f"... more rows not shown (limit {MAX_SQL_ROWS}); aggregate or add LIMIT/OFFSET.")
        if not rows:
            out.append("(no rows)")
        return "\n".join(out)

    @tool
    def interview(role: str, topic: str) -> str:
        """Interview one person about the company (at most 10 interviews per company).

        role: customer, current_employee, former_employee, competitor or supplier.
        topic: sales_team, working_conditions, top_management, decision_making or competitive_edge.
        People who would not know about a topic say so; the README lists who knows what.
        """
        role, topic = role.strip().lower(), topic.strip().lower()
        if role not in answers:
            return f"Unknown role {role!r}. Roles: {', '.join(answers)}. (Not counted.)"
        if topic not in answers[role]:
            return f"Unknown topic {topic!r}. Topics: {', '.join(answers[role])}. (Not counted.)"
        with lock:
            if INTERVIEWS_USED[name] >= INTERVIEW_LIMIT:
                return f"Interview limit reached: {INTERVIEW_LIMIT} interviews per company."
            INTERVIEWS_USED[name] += 1
        return answers[role][topic]

    return [list_documents, read_document, sql_query, interview]


def normalize_verdict(value: Any) -> str | None:
    """Accept PASS/FAIL (any case) or a boolean."""
    if isinstance(value, bool):
        return "PASS" if value else "FAIL"
    text = str(getattr(value, "content", value)).strip().upper()
    return text if text in VERDICTS else None


def check_chosen_points(chosen: Any) -> list[int]:
    """CHOSEN_POINTS must name 3 different points between 1 and 15."""
    try:
        points = sorted({int(p) for p in chosen})
    except (TypeError, ValueError):
        points = []
    if len(points) != 3 or len(chosen) != 3 or not all(p in FISHER_POINTS for p in points):
        raise SystemExit("Set CHOSEN_POINTS at the top of hw3.py to 3 different point numbers "
                         f"between 1 and 15, e.g. [1, 7, 15] (now: {chosen!r}).")
    return points


def normalize_points(points: Any, chosen: list[int]) -> dict[int, str | None]:
    """Map each chosen point number to PASS/FAIL, or None when missing/invalid."""
    points = points if isinstance(points, dict) else {}
    by_number: dict[int, Any] = {}
    for key, value in points.items():
        try:
            by_number[int(key)] = value
        except (TypeError, ValueError):
            continue
    return {p: normalize_verdict(by_number.get(p)) for p in chosen}


def read_ground_truth(folder: Path) -> dict[str, dict[str, Any]]:
    """Read the expected 15 point verdicts per company."""
    path = folder / "ground_truth.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def point_errors(points: dict[int, str | None], expected: dict[str, Any] | None) -> tuple[int, str]:
    """Count correct point verdicts and describe the wrong ones."""
    if expected is None:
        return 0, ""
    wrong = []
    for p in points:
        truth = expected["points"][str(p)]
        if points[p] != truth:
            wrong.append(f"{p}: expected {truth}, predicted {points[p] or 'missing'}")
    if all(v is None for v in points.values()):
        return 0, "all point verdicts missing"
    return len(points) - len(wrong), "; ".join(wrong)


def balanced_point_accuracy(
    analyses: dict[str, Any], truth: dict[str, dict[str, Any]], names: list[str], chosen: list[int]
) -> float:
    """Mean of the accuracy on true-PASS points and on true-FAIL points.

    Most verdicts are PASS, so answering PASS everywhere gets a high plain
    accuracy but only 50% balanced accuracy. Grading uses the balanced number.
    """
    hits = {"PASS": [0, 0], "FAIL": [0, 0]}
    for name in names:
        if name not in truth:
            continue
        entry = analyses.get(name)
        points = normalize_points(entry.get("points") if isinstance(entry, dict) else None, chosen)
        for p in chosen:
            expected = truth[name]["points"][str(p)]
            hits[expected][0] += points[p] == expected
            hits[expected][1] += 1
    rates = [h / n for h, n in hits.values() if n]
    return sum(rates) / len(rates) if rates else 0.0


def write_results(
    names: list[str], analyses: dict[str, Any], truth: dict[str, dict[str, Any]], chosen: list[int]
) -> tuple[Path, int]:
    """Write results.csv and return the number of correct point verdicts."""
    output = Path("results.csv")
    correct_points = 0
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["company", "points", "points_correct", "point_errors", "interviews_used", "summary"])
        for name in names:
            entry = analyses.get(name)
            entry = entry if isinstance(entry, dict) else {}
            points = normalize_points(entry.get("points"), chosen)
            expected = truth.get(name)
            n_points, errors = point_errors(points, expected)
            correct_points += n_points
            writer.writerow([
                name,
                " ".join(f"{p}:{points[p] or 'missing'}" for p in chosen),
                f"{n_points}/{len(chosen)}" if expected else "",
                errors,
                INTERVIEWS_USED.get(name, 0),
                str(entry.get("summary", "")).strip(),
            ])
    return output, correct_points


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run FTEC5660 HW3 on company data folders")
    parser.add_argument(
        "--data-folder",
        required=True,
        type=Path,
        help="folder containing one sub-folder per company",
    )
    return parser.parse_args()


async def run(folder: Path) -> int:
    folders = company_folders(folder)
    if not folders:
        raise SystemExit(f"no company folders found in {folder}")

    chosen = check_chosen_points(CHOSEN_POINTS)
    load_env_file()
    companies = {path.name: make_tools(path) for path in folders}
    agent = build_agent()
    analyses = await analyze_companies(agent, companies)
    if not isinstance(analyses, dict):
        raise TypeError("analyze_companies() must return a dictionary")

    truth = read_ground_truth(folder)
    output, correct_points = write_results(list(companies), analyses, truth, chosen)
    summary = f" Your points: {', '.join(map(str, chosen))}."
    if truth:
        summary += (
            f" Point accuracy: {correct_points}/{len(companies) * len(chosen)}"
            f" (balanced: {balanced_point_accuracy(analyses, truth, list(companies), chosen):.1%})."
        )
    print(f"Processed {len(companies)} compan{'y' if len(companies) == 1 else 'ies'}. Wrote {output}.{summary}")
    return 0


def main() -> int:
    args = parse_args()
    if not args.data_folder.is_dir():
        raise SystemExit(f"not a folder: {args.data_folder}")
    return asyncio.run(run(args.data_folder))


if __name__ == "__main__":
    raise SystemExit(main())
