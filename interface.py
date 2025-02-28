import streamlit as st
import asyncio
import logging
import os
from dotenv import load_dotenv
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from typing import Dict, Any

# Import from the renamed script
from report_generator import builder

# Load environment variables
load_dotenv()

# Configure logging (minimal output)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Custom CSS for modern, minimalistic styling
st.markdown("""
    <style>
    .stTextInput > div > input {
        border-radius: 8px;
        border: 1px solid #e0e0e0;
        padding: 10px;
        font-size: 16px;
    }
    .stTextArea > div > textarea {
        border-radius: 8px;
        border: 1px solid #e0e0e0;
        padding: 10px;
        font-size: 16px;
    }
    .stButton > button {
        border-radius: 8px;
        background-color: #007bff;
        color: white;
        padding: 8px 16px;
        font-size: 14px;
        border: none;
    }
    .stButton > button:hover {
        background-color: #0056b3;
    }
    .report-container {
        background-color: #f8f9fa;
        padding: 20px;
        border-radius: 10px;
        margin-top: 20px;
    }
    </style>
""", unsafe_allow_html=True)

# Custom async input handler for Streamlit
async def get_streamlit_input(prompt: str, key: str, placeholder: str = "Your feedback here...") -> str:
    st.markdown(prompt)
    feedback = st.text_area("", placeholder=placeholder, key=key, height=100, label_visibility="collapsed")
    if st.button("Submit", key=f"submit_{key}"):
        return feedback.strip() if feedback else ""
    return None

# Function to run the graph asynchronously
async def run_graph(graph_instance, input_data: Dict[str, Any], thread: Dict[str, Any]):
    state = {"topic": input_data["topic"], "feedback_on_report_plan": None}
    feedback_count = 0

    async for event in graph_instance.astream(input_data, thread, stream_mode="updates"):
        logger.info(f"Run graph event: {event}")
        if '__interrupt__' in event:
            interrupt_value = event['__interrupt__'][0].value
            st.session_state["current_prompt"] = interrupt_value
            st.session_state["state"] = graph_instance.get_state(thread).values
            st.session_state["feedback_key"] = f"feedback_{feedback_count}"
            return "awaiting_feedback"
        elif 'compile_final_report' in event:
            final_state = graph_instance.get_state(thread)
            report = final_state.values.get("final_report")
            st.session_state["final_report"] = report
            return "completed"
    return "running"

# Function to resume graph execution with feedback
async def resume_graph(graph_instance, thread: Dict[str, Any], feedback: str):
    update = {"feedback_on_report_plan": feedback if feedback.lower() != "true" else "true"}
    command = Command(resume=update if feedback.lower() != "true" else True)
    
    async for event in graph_instance.astream(command, thread, stream_mode="updates"):
        logger.info(f"Resume graph event: {event}")
        if '__interrupt__' in event:
            interrupt_value = event['__interrupt__'][0].value
            st.session_state["current_prompt"] = interrupt_value
            st.session_state["state"] = graph_instance.get_state(thread).values
            st.session_state["feedback_key"] = f"feedback_{st.session_state.get('feedback_count', 0) + 1}"
            return "awaiting_feedback"
        elif 'compile_final_report' in event:
            final_state = graph_instance.get_state(thread)
            report = final_state.values.get("final_report")
            st.session_state["final_report"] = report
            return "completed"
    return "running"

# Main Streamlit app
def main():
    st.title("Report Generator")
    st.markdown("Create detailed reports effortlessly with AI.")

    # Initialize session state
    if "stage" not in st.session_state:
        st.session_state["stage"] = "input"
        st.session_state["topic"] = ""
        st.session_state["current_prompt"] = ""
        st.session_state["final_report"] = ""
        st.session_state["feedback_count"] = 0
        st.session_state["state"] = {}
        st.session_state["feedback_key"] = "feedback_0"
        st.session_state["graph_instance"] = None
        st.session_state["loop"] = asyncio.new_event_loop()

    # Set the event loop
    asyncio.set_event_loop(st.session_state["loop"])

    # Thread configuration
    thread = {
        "configurable": {
            "thread_id": "streamlit_thread",
            "search_api": "tavily",
            "max_search_depth": 1,
        }
    }

    # Compile the graph once
    if st.session_state["graph_instance"] is None:
        memory = MemorySaver()
        st.session_state["graph_instance"] = builder.compile(checkpointer=memory)
        logger.info("Graph compiled with checkpointer")

    graph_instance = st.session_state["graph_instance"]

    # Stage 1: Topic Input
    if st.session_state["stage"] == "input":
        topic = st.text_input("", placeholder="Enter your topic...", label_visibility="collapsed")
        if st.button("Generate Report"):
            if topic:
                st.session_state["topic"] = topic
                st.session_state["stage"] = "generating"
                st.rerun()
            else:
                st.error("Please enter a topic.")

    # Stage 2: Generating Report Plan
    elif st.session_state["stage"] == "generating":
        with st.spinner("Generating your report plan..."):
            result = st.session_state["loop"].run_until_complete(
                run_graph(graph_instance, {"topic": st.session_state["topic"]}, thread)
            )
            st.session_state["stage"] = result
            st.rerun()

    # Stage 3: Awaiting Feedback
    elif st.session_state["stage"] == "awaiting_feedback":
        st.markdown("### Review Your Plan")
        st.write(st.session_state["current_prompt"])
        
        feedback = st.session_state["loop"].run_until_complete(
            get_streamlit_input(
                "Provide feedback or enter 'true' to proceed:",
                st.session_state["feedback_key"],
                "e.g., 'Add more details' or 'true'"
            )
        )

        if feedback is not None:
            st.session_state["feedback_count"] += 1
            with st.spinner("Updating your plan..."):
                result = st.session_state["loop"].run_until_complete(
                    resume_graph(graph_instance, thread, feedback)
                )
                st.session_state["stage"] = result
                st.rerun()

    # Stage 4: Completed Report
    elif st.session_state["stage"] == "completed":
        st.markdown("### Your Report")
        if st.session_state["final_report"]:
            st.markdown(
                f"<div class='report-container'>{st.session_state['final_report']}</div>",
                unsafe_allow_html=True
            )
            st.download_button(
                "Download Report",
                data=st.session_state["final_report"],
                file_name="report.md",
                mime="text/markdown"
            )
        else:
            st.error("Failed to generate the report.")
        
        if st.button("Start Over"):
            st.session_state.clear()
            st.rerun()

if __name__ == "__main__":
    main()