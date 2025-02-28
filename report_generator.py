import asyncio
import logging
import operator
import os
import re
import streamlit as st
from dataclasses import dataclass, fields
from enum import Enum
from typing import Annotated, Any, List, Literal, Optional, TypedDict

from dotenv import load_dotenv
import aiohttp
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.constants import Send
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langsmith import traceable
from pydantic import BaseModel, Field, ValidationError
from tavily import AsyncTavilyClient, TavilyClient

from prompts import (
    DEFAULT_REPORT_STRUCTURE,
    report_planner_query_writer_instructions,
    report_planner_instructions,
    query_writer_instructions,
    section_writer_instructions,
    section_grader_instructions,
    final_section_writer_instructions,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

llm_json = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash",
    temperature=0.5,
    api_key=st.secrets["GOOGLE_API_KEY"],
    response_mime_type="application/json"
)

llm_text = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash",
    temperature=0.5,
    api_key=st.secrets["GOOGLE_API_KEY"]
)

class SearchAPI(Enum):
    PERPLEXITY = "perplexity"
    TAVILY = "tavily"

@dataclass(kw_only=True)
class Configuration:
    report_structure: str = DEFAULT_REPORT_STRUCTURE
    number_of_queries: int = 2
    max_search_depth: int = 2
    search_api: SearchAPI = SearchAPI.TAVILY

    @classmethod
    def from_runnable_config(cls, config: Optional[RunnableConfig] = None) -> "Configuration":
        configurable = config["configurable"] if config and "configurable" in config else {}
        values: dict[str, Any] = {
            f.name: os.environ.get(f.name.upper(), configurable.get(f.name))
            for f in fields(cls)
            if f.init
        }
        return cls(**{k: v for k, v in values.items() if v})

class Section(BaseModel):
    name: str = Field(description="Name for this section of the report.")
    description: str = Field(description="Brief overview of the main topics and concepts to be covered in this section.")
    research: bool = Field(description="Whether to perform web research for this section of the report.")
    content: str = Field(description="The content of the section.")

class Sections(BaseModel):
    sections: List[Section] = Field(description="Sections of the report.")

class _SearchQuery(BaseModel):
    search_query: str = Field(None, description="Query for web search.")

class Queries(BaseModel):
    queries: List[_SearchQuery] = Field(description="List of search queries.")

class Feedback(BaseModel):
    grade: Literal["pass", "fail"] = Field(description="Evaluation result indicating whether the response meets requirements ('pass') or needs revision ('fail').")
    follow_up_queries: List[_SearchQuery] = Field(description="List of follow-up search queries.")

class ReportStateInput(TypedDict):
    topic: str

class ReportStateOutput(TypedDict):
    final_report: str

class ReportState(TypedDict):
    topic: str
    feedback_on_report_plan: str
    sections: list[Section]
    completed_sections: Annotated[list, operator.add]
    report_sections_from_research: str
    final_report: str

class SectionState(TypedDict):
    section: Section
    search_iterations: int
    search_queries: list[_SearchQuery]
    source_str: str
    report_sections_from_research: str
    completed_sections: list[Section]

class SectionOutputState(TypedDict):
    completed_sections: list[Section]

tavily_client = TavilyClient(api_key=st.secrets["TAVILY_API_KEY"])
tavily_async_client = AsyncTavilyClient(api_key=st.secrets["TAVILY_API_KEY"])

def get_config_value(value):
    return value if isinstance(value, str) else value.value

def deduplicate_and_format_sources(search_response, max_tokens_per_source, include_raw_content=True):
    sources_list = []
    for response in search_response:
        sources_list.extend(response['results'])

    unique_sources = {source['url']: source for source in sources_list}

    formatted_text = "Sources:\n\n"
    for i, source in enumerate(unique_sources.values(), 1):
        formatted_text += f"Source {source['title']}:\n===\n"
        formatted_text += f"URL: {source['url']}\n===\n"
        formatted_text += f"Most relevant content from source: {source['content']}\n===\n"
        if include_raw_content:
            char_limit = max_tokens_per_source * 4
            raw_content = source.get('raw_content', '')
            if raw_content is None:
                raw_content = ''
                logger.warning(f"No raw_content found for source: {source['url']}")
            if len(raw_content) > char_limit:
                raw_content = raw_content[:char_limit] + "... [truncated]"
            formatted_text += f"Full source content limited to {max_tokens_per_source} tokens: {raw_content}\n\n"

    return formatted_text.strip()

def format_sections(sections: list[Section]) -> str:
    formatted_str = ""
    for idx, section in enumerate(sections, 1):
        formatted_str += f"""
{'='*60}
Section {idx}: {section.name}
{'='*60}
Description:
{section.description}
Requires Research:
{section.research}

Content:
{section.content if section.content else '[Not yet written]'}

"""
    return formatted_str

@traceable
async def tavily_search_async(search_queries):
    search_tasks = []
    for query in search_queries:
        search_tasks.append(
            tavily_async_client.search(
                query.search_query,
                max_results=5,
                include_raw_content=True,
                topic="general"
            )
        )

    try:
        search_docs = await asyncio.gather(*search_tasks)
        return search_docs
    except Exception as e:
        logger.error(f"Error in Tavily search: {e}")
        return []

@traceable
async def perplexity_search(search_queries):
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "Authorization": f"Bearer {st.secrets['PERPLEXITY_API_KEY']}"
    }

    async with aiohttp.ClientSession() as session:
        search_docs = []
        for query in search_queries:
            payload = {
                "model": "sonar-pro",
                "messages": [
                    {"role": "system", "content": "Search the web and provide factual information with sources."},
                    {"role": "user", "content": query.search_query}
                ]
            }
            try:
                async with session.post(
                    "https://api.perplexity.ai/chat/completions",
                    headers=headers,
                    json=payload
                ) as response:
                    response.raise_for_status()
                    data = await response.json()
                    content = data["choices"][0]["message"]["content"]
                    citations = data.get("citations", ["https://perplexity.ai"])

                    results = []
                    results.append({
                        "title": "Perplexity Search, Source 1",
                        "url": citations[0],
                        "content": content,
                        "raw_content": content,
                        "score": 1.0
                    })
                    for i, citation in enumerate(citations[1:], start=2):
                        results.append({
                            "title": f"Perplexity Search, Source {i}",
                            "url": citation,
                            "content": "See primary source for full content",
                            "raw_content": None,
                            "score": 0.5
                        })
                    search_docs.append({
                        "query": query.search_query,
                        "follow_up_questions": None,
                        "answer": None,
                        "images": [],
                        "results": results
                    })
            except Exception as e:
                logger.error(f"Error in Perplexity search for query '{query.search_query}': {e}")
                search_docs.append({
                    "query": query.search_query,
                    "follow_up_questions": None,
                    "answer": None,
                    "images": [],
                    "results": []
                })
        return search_docs

async def generate_report_plan(state: ReportState, config: RunnableConfig):
    logger.info("Generating report plan...")
    topic = state["topic"]
    feedback = state.get("feedback_on_report_plan", None)

    configurable = Configuration.from_runnable_config(config)
    report_structure = configurable.report_structure
    number_of_queries = configurable.number_of_queries

    if isinstance(report_structure, dict):
        report_structure = str(report_structure)

    structured_llm_queries = llm_json.with_structured_output(Queries)
    system_instructions_query = report_planner_query_writer_instructions.format(
        topic=topic, report_organization=report_structure, number_of_queries=number_of_queries
    )
    try:
        results = await structured_llm_queries.ainvoke(
            [SystemMessage(content=system_instructions_query)] +
            [HumanMessage(content="Generate search queries that will help with planning the sections of the report.")]
        )
        query_list = [_SearchQuery(search_query=query.search_query) for query in results.queries]
    except Exception as e:
        logger.error(f"Error generating search queries: {e}")
        query_list = []

    search_api = get_config_value(configurable.search_api)
    if search_api == "tavily":
        search_results = await tavily_search_async(query_list)
        source_str = deduplicate_and_format_sources(search_results, max_tokens_per_source=1000, include_raw_content=False)
    elif search_api == "perplexity":
        search_results = await perplexity_search(query_list)
        source_str = deduplicate_and_format_sources(search_results, max_tokens_per_source=1000, include_raw_content=False)
    else:
        raise ValueError(f"Unsupported search API: {configurable.search_api}")

    system_instructions_sections = report_planner_instructions.format(
        topic=topic, report_organization=report_structure, context=source_str, feedback=feedback
    )

    human_message = """
    Generate the sections of the report in JSON format. The JSON must have a 'sections' key containing a list of sections. Each section must include all four fields: 'name', 'description', 'research', and 'content'. Set 'content' to an empty string (""). For example:

    {
      "sections": [
        {
          "name": "Introduction",
          "description": "Brief overview of the topic area",
          "research": false,
          "content": ""
        },
        {
          "name": "Main Section",
          "description": "Detailed analysis of the topic",
          "research": true,
          "content": ""
        },
        {
          "name": "Conclusion",
          "description": "Summary of findings",
          "research": false,
          "content": ""
        }
      ]
    }

    Ensure every section has all four fields as shown. Do not omit 'description' or 'content'.
    Please generate the sections for the report on the given topic.
    """

    try:
        raw_response = await llm_json.ainvoke(
            [SystemMessage(content=system_instructions_sections)] +
            [HumanMessage(content=human_message)]
        )
        json_str = raw_response.content
        logger.info(f"Raw LLM output: {json_str}")
    except Exception as e:
        logger.error(f"Error generating report sections: {e}")
        return {"sections": []}

    match = re.search(r'```json\n(.*?)\n```', json_str, re.DOTALL)
    if match:
        json_content = match.group(1)
    else:
        json_content = json_str
        logger.warning("Could not extract JSON from markdown code block. Using raw string.")
    
    logger.info(f"Extracted JSON content: {json_content}")

    try:
        report_sections = Sections.model_validate_json(json_content)
        sections = report_sections.sections
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        sections = []

    return {"sections": sections}

def human_feedback(state: ReportState, config: RunnableConfig):
    sections = state['sections']
    sections_str = "\n\n".join(
        f"Section: {section.name}\n"
        f"Description: {section.description}\n"
        f"Research needed: {'Yes' if section.research else 'No'}\n"
        for section in sections
    )

    feedback = interrupt(
        f"Please provide feedback on the following report plan:\n\n{sections_str}\n\n"
        "Does the report plan meet your needs? Enter 'true' to approve, or provide feedback as a string to regenerate the plan:"
    )

    return {"feedback_on_report_plan": feedback}

def generate_queries(state: SectionState, config: RunnableConfig):
    logger.info("Generating search queries...")
    section = state["section"]
    configurable = Configuration.from_runnable_config(config)
    number_of_queries = configurable.number_of_queries

    structured_llm = llm_json.with_structured_output(Queries)
    system_instructions = query_writer_instructions.format(section_topic=section.description, number_of_queries=number_of_queries)
    try:
        queries = structured_llm.invoke(
            [SystemMessage(content=system_instructions)] +
            [HumanMessage(content="Generate search queries on the provided topic.")]
        )
        return {"search_queries": queries.queries}
    except Exception as e:
        logger.error(f"Error generating queries: {e}")
        return {"search_queries": []}

async def search_web(state: SectionState, config: RunnableConfig):
    logger.info("Searching the web...")
    search_queries = state["search_queries"]
    configurable = Configuration.from_runnable_config(config)

    query_list = [_SearchQuery(search_query=query.search_query) for query in search_queries]
    search_api = get_config_value(configurable.search_api)

    if search_api == "tavily":
        search_results = await tavily_search_async(query_list)
        source_str = deduplicate_and_format_sources(search_results, max_tokens_per_source=5000, include_raw_content=True)
    elif search_api == "perplexity":
        search_results = await perplexity_search(query_list)
        source_str = deduplicate_and_format_sources(search_results, max_tokens_per_source=5000, include_raw_content=False)
    else:
        raise ValueError(f"Unsupported search API: {configurable.search_api}")

    return {"source_str": source_str, "search_iterations": state["search_iterations"] + 1}

def write_section(state: SectionState, config: RunnableConfig) -> Command[Literal[END, "search_web"]]:
    logger.info("Writing section...")
    section = state["section"]
    source_str = state["source_str"]
    configurable = Configuration.from_runnable_config(config)
    
    system_instructions = section_writer_instructions.format(
        section_title=section.name, section_topic=section.description, context=source_str, section_content=section.content
    )

    try:
        section_content = llm_text.invoke(
            [SystemMessage(content=system_instructions)] +
            [HumanMessage(content="Generate a report section based on the provided sources.")]
        )
        section.content = section_content.content
    except Exception as e:
        logger.error(f"Error writing section: {e}")
        section.content = "[Error generating content]"

    section_grader_instructions_formatted = section_grader_instructions.format(
        section_topic=section.description, section=section.content
    )

    structured_llm = llm_json.with_structured_output(Feedback)
    try:
        feedback = structured_llm.invoke(
            [SystemMessage(content=section_grader_instructions_formatted)] +
            [HumanMessage(content="Grade the report and consider follow-up questions for missing information:")]
        )
    except Exception as e:
        logger.error(f"Error grading section: {e}")
        feedback = Feedback(grade="fail", follow_up_queries=[])

    if feedback.grade == "pass" or state["search_iterations"] >= configurable.max_search_depth:
        return Command(update={"completed_sections": [section]}, goto=END)
    else:
        return Command(update={"search_queries": feedback.follow_up_queries, "section": section}, goto="search_web")

def write_final_sections(state: SectionState, config: RunnableConfig):
    logger.info("Writing final sections...")
    configurable = Configuration.from_runnable_config(config)
    section = state["section"]
    completed_report_sections = state["report_sections_from_research"]
    
    system_instructions = final_section_writer_instructions.format(
        section_title=section.name, section_topic=section.description, context=completed_report_sections
    )

    try:
        section_content = llm_text.invoke(
            [SystemMessage(content=system_instructions)] +
            [HumanMessage(content="Generate a report section based on the provided sources.")]
        )
        section.content = section_content.content
    except Exception as e:
        logger.error(f"Error writing final section: {e}")
        section.content = "[Error generating content]"

    return {"completed_sections": [section]}

def gather_completed_sections(state: ReportState):
    logger.info("Gathering completed sections...")
    completed_sections = state["completed_sections"]
    completed_report_sections = format_sections(completed_sections)
    return {"report_sections_from_research": completed_report_sections}

def initiate_final_section_writing(state: ReportState):
    logger.info("Initiating final section writing...")
    return [
        Send("write_final_sections", {"section": s, "report_sections_from_research": state["report_sections_from_research"]})
        for s in state["sections"]
        if not s.research
    ]

def compile_final_report(state: ReportState):
    logger.info("Compiling final report...")
    sections = state["sections"]
    completed_sections = {s.name: s.content for s in state["completed_sections"]}

    for section in sections:
        if section.name in completed_sections:
            section.content = completed_sections[section.name]
        else:
            logger.warning(f"Section '{section.name}' not completed.")
            section.content = "[Section not completed]"

    all_sections = "\n\n".join([s.content for s in sections])

    with open("data/final_report.md", "w") as f:
        f.write(all_sections)

    return {"final_report": all_sections}

section_builder = StateGraph(SectionState, output=SectionOutputState)
section_builder.add_node("generate_queries", generate_queries)
section_builder.add_node("search_web", search_web)
section_builder.add_node("write_section", write_section)

section_builder.add_edge(START, "generate_queries")
section_builder.add_edge("generate_queries", "search_web")
section_builder.add_edge("search_web", "write_section")

builder = StateGraph(ReportState, input=ReportStateInput, output=ReportStateOutput, config_schema=Configuration)
builder.add_node("generate_report_plan", generate_report_plan)
builder.add_node("human_feedback", human_feedback)
builder.add_node("build_section_with_web_research", section_builder.compile())
builder.add_node("gather_completed_sections", gather_completed_sections)
builder.add_node("write_final_sections", write_final_sections)
builder.add_node("compile_final_report", compile_final_report)

builder.add_edge(START, "generate_report_plan")
builder.add_edge("generate_report_plan", "human_feedback")

def route_after_feedback(state: ReportState):
    feedback = state.get("feedback_on_report_plan")
    logger.info(f"Routing based on feedback: {feedback}")
    if feedback == "true" or feedback is True:
        sends = [
            Send("build_section_with_web_research", {"section": s, "search_iterations": 0})
            for s in state["sections"]
            if s.research
        ]
        logger.info(f"Sending to build_section_with_web_research: {len(sends)} sections")
        return sends
    else:
        logger.info("Regenerating report plan due to feedback.")
        return "generate_report_plan"

builder.add_conditional_edges("human_feedback", route_after_feedback, {
    "generate_report_plan": "generate_report_plan",
})

builder.add_edge("build_section_with_web_research", "gather_completed_sections")
builder.add_conditional_edges("gather_completed_sections", initiate_final_section_writing, ["write_final_sections"])
builder.add_edge("write_final_sections", "compile_final_report")
builder.add_edge("compile_final_report", END)

graph = builder.compile()
mermaid_png = graph.get_graph(xray=1).draw_mermaid_png()
with open("graph_visualization.png", "wb") as f:
    f.write(mermaid_png)

async def get_terminal_input(prompt: str) -> str:
    print(prompt)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, input, "Your feedback: ")

if __name__ == "__main__":
    from langgraph.checkpoint.memory import MemorySaver

    async def run_example():
        memory = MemorySaver()
        graph_instance = builder.compile(checkpointer=memory)
        thread = {
            "configurable": {
                "thread_id": "example_thread",
                "search_api": "tavily",
                "max_search_depth": 1,
            }
        }
        topic = "Overview of the AI inference market with focus on Fireworks, Together.ai, Groq"

        logger.info("Starting graph execution...")
        async for event in graph_instance.astream({"topic": topic}, thread, stream_mode="updates"):
            logger.info(f"Initial event: {event}")
            if '__interrupt__' in event:
                interrupt_value = event['__interrupt__'][0].value
                print(interrupt_value)
                break

        while True:
            feedback = await get_terminal_input("Enter your feedback:")
            logger.info(f"Received feedback: {feedback}")

            if feedback.lower() == "true":
                logger.info("Feedback 'true' received, resuming to complete report...")
                async for event in graph_instance.astream(
                    Command(resume=True), thread, stream_mode="updates"
                ):
                    logger.info(f"Resumption event: {event}")
                    if 'compile_final_report' in event:
                        final_state = graph_instance.get_state(thread)
                        report = final_state.values.get("final_report")
                        if report:
                            logger.info(f"Final report generated:\n{report}")
                            print("Final report:\n", report)
                        else:
                            logger.error("Final report is empty!")
                        return
            else:
                logger.info("Resuming with feedback to regenerate plan...")
                async for event in graph_instance.astream(
                    Command(resume=feedback), thread, stream_mode="updates"
                ):
                    logger.info(f"Feedback event: {event}")
                    if '__interrupt__' in event:
                        interrupt_value = event['__interrupt__'][0].value
                        print(interrupt_value)
                        break

    asyncio.run(run_example())