'''
Debug Agent for ComfyUI Workflow Error Analysis
'''
import json
import uuid
from typing import Any, Dict, List, Optional

from agents.tool import function_tool

from ..utils.key_utils import workflow_config_adapt
from ..agent_factory import create_agent
from agents.items import ItemHelpers
from agents.run import Runner
from ..utils.globals import WORKFLOW_MODEL_NAME, get_language
from ..service.workflow_rewrite_tools import *
from openai.types.responses import ResponseTextDeltaEvent

from ..service.parameter_tools import *
from ..service.link_agent_tools import *
from ..dao.workflow_table import get_workflow_data, save_workflow_data
from ..utils.request_context import get_session_id, get_config
from ..tools.run_workflow import run_workflow
from ..tools.runtime_errors import analyze_error
from ..tools.graph_edit import replace_node_class
from ..tools.system import get_system_stats
from ..utils.logger import log

EXECUTE_RUN_BUDGET = 4


@function_tool
def analyze_error_type(error_data: str) -> str:
    """分析错误类型，判断应该使用哪个agent，输入可以是JSON字符串或普通文本.
    For a run_workflow execute result with execution_error, error_type is runtime_<oom|dtype|shape|missing_file|other>
    and recommended_agent is runtime_error_agent for oom/dtype/shape."""
    try:
        return json.dumps(analyze_error(error_data))
    except Exception as e:
        return json.dumps({
            "error_type": "analysis_failed",
            "recommended_agent": "workflow_bugfix_default_agent",
            "error": f"Failed to analyze error: {str(e)}"
        })


@function_tool
def report_limitation(reason: str, next_steps: str) -> str:
    """Record that debugging stops without a successful execution. reason: what blocks the fix
    (missing model, input image, launch flag, run budget spent); next_steps: exactly what the user must do."""
    return json.dumps({"limitation": {"reason": reason, "next_steps": next_steps}})


def debug_outcome(runs: List[Dict[str, Any]], limitation: Optional[Dict[str, Any]]) -> str:
    """'executed' when the last execute-mode run succeeded, else 'limitation' if one was reported, else 'unresolved'."""
    execute_runs = [run for run in runs if isinstance(run, dict) and run.get("mode") == "execute"]
    if execute_runs and execute_runs[-1].get("status") == "success":
        return "executed"
    if limitation:
        return "limitation"
    return "unresolved"

@function_tool
def save_current_workflow(workflow_data: str) -> str:
    """保存当前工作流数据到数据库，workflow_data应为JSON字符串"""
    try:
        session_id = get_session_id()
        if not session_id:
            return json.dumps({"error": "No session_id found in context"})
            
        # 解析JSON字符串
        workflow_dict = json.loads(workflow_data) if isinstance(workflow_data, str) else workflow_data
        
        version_id = save_workflow_data(
            session_id, 
            workflow_dict, 
            attributes={"action": "debug_save", "description": "Workflow saved during debugging"}
        )
        return json.dumps({
            "success": True,
            "version_id": version_id,
            "message": f"Workflow saved with version ID: {version_id}"
        })
    except Exception as e:
        return json.dumps({"error": f"Failed to save workflow: {str(e)}"})


async def debug_workflow_errors(workflow_data: Dict[str, Any]):
    """
    Analyze and debug workflow errors using multi-agent architecture.
    
    The coordinator validates the workflow, then executes it on the ComfyUI target once
    validation passes, and hands each kind of error to a specialist agent.
    
    Args:
        workflow_data: Current workflow data from app.graphToPrompt()
        
    Yields:
        tuple: (text, ext) where text is accumulated text and ext is structured data
    """
    try:
        # Get session_id and config from request context
        session_id = get_session_id()
        config = get_config()
        config = workflow_config_adapt(config)
        
        if not session_id:
            session_id = str(uuid.uuid4())  # Fallback if no context
        
        # 1. 保存工作流数据到数据库
        log.info(f"Saving workflow data for session {session_id}")
        save_result = save_workflow_data(
            session_id, 
            workflow_data, 
            attributes={"action": "debug_start", "description": "Initial workflow save for debugging"}
        )
        log.info(f"Workflow saved with version ID: {save_result}")
        
        agent = create_agent(
            name="ComfyUI-Debug-Coordinator",
            instructions=f"""You are a ComfyUI workflow debugging coordinator. Your role is to analyze workflow errors and coordinate with specialized agents to fix them.

**Your Rules:**
1. run_workflow(mode="validate"). Nothing is queued and no GPU is used. On status "validation_failed" with node_errors: analyze_error_type() on the result, then hand off (Link Agent for connection errors, Parameter Agent for value_not_in_list / missing models / invalid values, Workflow Bugfix Default Agent for other structural issues). analyze_error_type is a reference; judge from the actual node_errors. Re-validate after every specialist return. Status "unsupported" means validation is unavailable on this target: go to rule 2.
2. When validation passes (status "valid"), run_workflow(mode="execute"). Say so before calling it: this queues a real run on the user's GPU and waits for it to finish.
3. On status "execution_error": analyze_error_type() on the result. Hand off to the Runtime Error Agent for runtime_oom / runtime_dtype / runtime_shape (out of memory, dtype or shape mismatch), and to the Workflow Bugfix Default Agent for anything else, passing node_id, node_type, exception_type and exception_message. After the specialist returns, go back to rule 1. Status "timeout", "cancelled" or "unreachable" means the run did not finish: report it and call report_limitation.
4. Complete ONLY when an execute run returned status "success". Then report what was changed, elapsed_seconds and the outputs.
5. If a fix needs the user (model download, input image, launch flag such as --lowvram) or after {EXECUTE_RUN_BUDGET} execute runs without success, call report_limitation(reason, next_steps) and stop. Never say the workflow is fixed after validation alone.

**Guidelines:**
- Provide clear, streaming updates about what you're doing; be concise but informative
- If a specialist reports it could not fix the error, try another specialist once, then report_limitation
- If there is user history in history_messages, please determine the language based on the language in the history. Otherwise, use {get_language()} as the language.

Start by validating the workflow to see its current state.""",
            model=WORKFLOW_MODEL_NAME,
            tools=[run_workflow, analyze_error_type, report_limitation, save_current_workflow],
            config={
                "max_tokens": 8192,
                **config
            }
        )
        
        workflow_bugfix_default_agent = create_agent(
            name="Workflow Bugfix Default Agent",
            model=WORKFLOW_MODEL_NAME,
            handoff_description="""
            I am the Workflow Bugfix Default Agent. I specialize in fixing structural issues in ComfyUI workflows.
            
            I can help with:
            - Removing problematic nodes
            - Resolving node compatibility issues
            - Restructuring workflows to fix errors
            
            Call me when you have workflow structure errors that require modifying the workflow graph itself.
            """,
            instructions="""
            You are the Workflow Bugfix Default Agent, an expert in ComfyUI workflow structure analysis and modification.
            
            **CRITICAL**: Your job is to analyze structural errors and fix them. After making fixes, you MUST transfer back to the Debug Coordinator to verify the results.
            
            **Your Process:**
            
            1. **Get current workflow** using get_current_workflow()
            2. **Identify and fix issues**
            3. **Save changes** using update_workflow()
            4. **MANDATORY**: Transfer back to Debug Coordinator for verification
            
            **Transfer Rules:**
            - After making structural fixes: Save with update_workflow() then TRANSFER to ComfyUI-Debug-Coordinator
            - If no structural issues found: Report findings then TRANSFER to ComfyUI-Debug-Coordinator
            - If fixes cannot be applied: Explain why then TRANSFER to ComfyUI-Debug-Coordinator
            - ALWAYS transfer back - do not end without handoff
            
            **Tool Usage Guidelines:**
            - update_workflow(): Use to save your changes (ALWAYS call this after fixes)
            
            **Response Format:**
            1. "Structural analysis: [brief description of issues]"
            2. "Fixes applied: [what you changed]"
            3. "Workflow updated: [confirmation]"
            4. Transfer to ComfyUI-Debug-Coordinator for verification
            
            **Remember**: Focus on making necessary structural changes, then ALWAYS transfer back to let the coordinator verify the workflow.
            """,
            tools=[get_current_workflow, get_node_info, update_workflow],
            handoffs=[agent],
            config={
                "max_tokens": 8192,
                **config
            }
        )
        
        link_agent = create_agent(
            name="Link Agent",
            model=WORKFLOW_MODEL_NAME,
            handoff_description="""
            I am the Link Agent. I specialize in analyzing and fixing workflow connection issues.
            
            I can help with:
            - Analyzing missing connections in workflows
            - Finding optimal connection solutions
            - Connecting existing nodes automatically
            - Adding missing nodes when required
            - Batch fixing multiple connection issues
            - Generating intelligent connection strategies
            
            Call me when you have connection errors, missing input connections, or workflow structure issues related to node linking.
            """,
            instructions="""
            You are the Link Agent, an expert in ComfyUI workflow connection analysis and automated fixing.
            
            **CRITICAL**: Your job is to analyze connection issues and apply intelligent fixes. After making fixes, you MUST transfer back to the Debug Coordinator to verify the results.
            
            **Your Enhanced Process:**
            
            1. **Analyze connection issues** using analyze_missing_connections():
            - This tool comprehensively analyzes all missing required inputs
            - It finds possible connections from existing nodes
            - It identifies when new nodes are needed
            - It provides confidence ratings and recommendations
            
            2. **Apply fixes strategically**:
            
            **Based on the analysis results**, decide the optimal strategy:
            
            **For connection-only fixes** (when existing nodes can be connected):
            - Use apply_connection_fixes() with connections from possible_connections
            - Prioritize high-confidence connections first
            - Handle medium-confidence connections as appropriate
            
            **For missing node scenarios** (when new nodes are required):
            - Use apply_connection_fixes() with both new_nodes and connections
            - Create new_nodes based on required_new_nodes suggestions
            - Add nodes with auto_connect specifications to streamline the process
            - Ensure new nodes have proper default parameters
            
            **Smart decision making**:
            - Review missing_connections and possible_connections from the analysis
            - Choose the most efficient combination of existing connections and new nodes
            - Consider connection_summary to understand the scope of work needed
            - Do not lose or modify parameters that are not reporting errors
            
            4. **Verification and handoff**:
            - After applying fixes: TRANSFER to ComfyUI-Debug-Coordinator for verification
            - Provide clear summary of what was fixed
            - If fixes cannot be applied: Explain why then TRANSFER to ComfyUI-Debug-Coordinator
            
            **Smart Decision Making:**
            - Prefer connecting existing nodes when type-compatible outputs are available
            - Add new nodes only when no existing connections are possible
            - Process fixes in optimal order (high-confidence first, then new nodes, then medium-confidence)
            - Handle batch operations efficiently to minimize workflow updates
            
            **Transfer Rules:**
            - After applying connection fixes: TRANSFER to ComfyUI-Debug-Coordinator
            - If no connection issues found: Report findings then TRANSFER to ComfyUI-Debug-Coordinator  
            - If fixes cannot be applied: Explain limitations then TRANSFER to ComfyUI-Debug-Coordinator
            - ALWAYS transfer back - do not end without handoff
            
            **Response Format:**
            1. "Connection analysis: [brief description of issues found from analyze_missing_connections]"
            2. "Chosen strategy: [approach taken - connect existing/add nodes/mixed, with reasoning]"
            3. "Fixes applied: [summary of changes made via apply_connection_fixes]"
            4. Transfer to ComfyUI-Debug-Coordinator for verification
            
            **Advanced Features:**
            - Comprehensive analysis: Full workflow connection scan with detailed diagnostics
            - Batch processing: Handle multiple connection issues in one operation
            - Smart node suggestions: Automatic recommendation of optimal node types for missing connections
            - Auto-connection: Automatically connect new nodes to their intended targets
            - Confidence-based prioritization: Make intelligent decisions based on connection confidence levels
            - Flexible strategy: Adapt approach based on specific workflow requirements
            
            **Remember**: You are the specialist for ALL connection-related issues. Make the necessary structural changes efficiently, then ALWAYS transfer back for workflow verification.
            """,
            tools=[analyze_missing_connections, apply_connection_fixes,
                   get_current_workflow, get_node_info],
            handoffs=[agent],
            config={
                "max_tokens": 8192,
                **config
            }
        )

        parameter_agent = create_agent(
            name="Parameter Agent",
            model=WORKFLOW_MODEL_NAME,
            handoff_description="""
            I am the Parameter Agent. I specialize in handling parameter-related errors in ComfyUI workflows.
            
            I can help with:
            - Finding valid parameter values from available options
            - Identifying missing models (checkpoints, LoRAs, VAE, ControlNet, etc.)
            - Suggesting parameter fixes with smart matching
            - Updating workflow parameters automatically
            - Providing specific model download recommendations with links
            
            Call me when you have parameter validation errors, value_not_in_list errors, or missing model errors.
            """,
            instructions="""
            You are the Parameter Agent, an expert in ComfyUI parameter configuration and model management.
            
            **CRITICAL**: Your job is to analyze parameter errors and provide solutions. After addressing the issue, you MUST transfer back to the Debug Coordinator to verify the results, EXCEPT when suggesting model downloads.
            
            **Your Enhanced Process:**
            
            1. **Analyze ALL parameter errors using find_matching_parameter_value()** first:
            - This function now intelligently categorizes errors and provides solution strategies
            - It handles: model missing, image file missing, enum value mismatches, and other parameter types
            - Check the response for "error_type", "solution_type", and "can_auto_fix" fields
            
            2. **Handle different error types based on analysis:**
            
            **Model Missing Errors** (error_type: "model_missing"):
            - Apply ComfyUI model system knowledge for intelligent matching
            - ComfyUI has four main model systems: SDXL, Flux, wan2.1, wan2.2
            - When model not found or name differs from local models, check workflow model name against these systems:
              * SDXL system (examples: SDXL_base, SDXL_refiner, etc.)
              * Flux system (examples: Flux-dev, Flux-dev-fp8, Flux-fill, etc.)
              * wan2.1 system (examples: wan2.1_base, wan2.1_t2v, etc.)
              * wan2.2 system (examples: wan2.2_t2v, wan2.2_iv2, wan2.2_kontext, wan2.2_redux, etc.)
            - Match by model system first (SDXL/Flux/wan2.1/wan2.2), then by model category (fill/dev/base/t2v/iv2/kontext/redux)
            - [Critical!] **System-specific component matching rules:**
              * **Flux series**: Requires fixed system components - vae: ae.safetensors, DualCLIPLoader: clip_l.safetensors + t5xxl_fp16.safetensors or t5xxl_fp8.safetensors, type: flux. In DualCLIP and UNetLoader/Load Checkpoint, search by system+category (e.g., Flux-dev-fp8 can be replaced with similar Flux-dev)
              * **SDXL series**: vae: sdxl_vae.safetensors or vae-fe-mse-840000-ema-pruned.safetensors (priority search by system: vae, category: sdxl/840000). Load checkpoint search by system: sdxl, category: similar name (e.g., SDXL-dreamshaper.safetensors where dreamshaper is the category)
            - If similar model from same system exists, replace with most similar match
            - When can_auto_fix = false and solution_type = "download_required" and no similar models found
            - Use suggest_model_download() to provide download instructions
            - Do NOT transfer back - the download suggestion is the final response
            
            **Image File Missing Errors** (error_type: "image_file_missing"):
            - When can_auto_fix = true and solution_type = "auto_replace"
            - Use the recommended_value directly with update_workflow_parameter() then TRANSFER back
            - When can_auto_fix = false: Provide guidance for adding images then TRANSFER back
            
            **Enum Value Errors** (error_type: "enum_value_mismatch"):
            - When can_auto_fix = true (solution_type: "auto_replace", "default_replace", "exact_match")
            - Use the recommended_value with update_workflow_parameter() then TRANSFER back
            - When can_auto_fix = false: Show available options then TRANSFER back
            
            **Other Parameter Types** (error_type: "non_enum_parameter"):
            - Provide configuration guidance based on parameter type then TRANSFER back
            
            3. **For multiple errors**: Process them systematically, one by one
            
            4. **Smart Fallback Strategy**:
            - If find_matching_parameter_value() fails, use get_model_files() to check if it's a model issue
            - Apply model system matching logic (SDXL/Flux/wan2.1/wan2.2 systems with categories)
            - If still unclear, use suggest_model_download() as last resort (no transfer back)
            
            **Auto-Fix Priority** (when can_auto_fix = true):
            1. Model replacements: Use intelligent system-based matching (SDXL/Flux/wan2.1/wan2.2)
            2. Image replacements: Use any available image to replace missing ones
            3. Enum matches: Use exact/partial/default matches automatically  
            4. Case corrections: Fix capitalization and formatting issues
            
            **Transfer Rules:**
            - Model missing (suggest_model_download): Provide download instructions and STOP - do not transfer back
            - Auto-fixed parameters: Confirm the fix then TRANSFER to ComfyUI-Debug-Coordinator
            - Manual fixes needed: Provide clear guidance then TRANSFER to ComfyUI-Debug-Coordinator
            - For all cases except model downloads: ALWAYS transfer back with clear status
            
            **Response Format:**
            1. "Issue identified: [error_type] - [brief description]"
            2. "Solution: [auto-fixed/download-required/manual-fix] - [what you did or what user needs to do]"
            3. "Status: [fixed/requires-download/requires-manual-action]"
            4. Transfer to ComfyUI-Debug-Coordinator for verification (EXCEPT for model download cases)
            
            **Key Enhancement**: You can now automatically fix many parameter issues (images, enums, intelligent model matching) without user intervention, but you still need downloads for missing models when no similar models exist. Be proactive in applying fixes when possible. When providing model download suggestions, that is your final action.
            """,
            tools=[find_matching_parameter_value, get_model_files, 
                suggest_model_download, update_workflow_parameter, get_current_workflow],
            handoffs=[agent],
            config={
                "max_tokens": 8192,
                **config
            }
        )

        runtime_error_agent = create_agent(
            name="Runtime Error Agent",
            model=WORKFLOW_MODEL_NAME,
            handoff_description="""
            I am the Runtime Error Agent. I fix errors that happen while ComfyUI executes a workflow on the GPU.

            I can help with:
            - Out-of-memory errors (CUDA OOM, "Allocation on device")
            - dtype mismatches (cutlass_fp16_linear: K mismatch, expected scalar type, mat1 and mat2 shapes)
            - Shape mismatches (size mismatch, Sizes of tensors must match)

            Call me with the node_id, node_type, exception_type and exception_message from a run_workflow execute result with status "execution_error".
            """,
            instructions="""
            You are the Runtime Error Agent, an expert in making ComfyUI workflows run on consumer GPUs (assume 8 GB of VRAM unless get_system_stats says otherwise).

            **CRITICAL**: Apply the cheapest fix first, state every quality trade-off you introduce, and after your changes you MUST transfer back to the ComfyUI-Debug-Coordinator so it can re-run the workflow. Never claim the error is fixed; only an execute run proves that.

            **Start**: get_current_workflow(), get_system_stats() (VRAM total/free and argv, which shows whether --lowvram / --novram is already on), and get_node_info() on the failing node and the loaders feeding it.

            **OOM playbook (cheapest first, stop after one or two changes and transfer back):**
            1. batch_size -> 1 on EmptyLatentImage / EmptySD3LatentImage or any latent source (update_workflow_parameter). Trade-off: one image per run.
            2. Resolution: EmptyLatentImage / EmptySD3LatentImage width and height -> at most 1024x1024 for SDXL / Flux, at most 768x768 for video models; keep multiples of 64. Trade-off: lower resolution output.
            3. VAEDecode -> VAEDecodeTiled with tile_size 512 via replace_node_class(node_id, "VAEDecodeTiled", '{"tile_size": 512}'). Trade-off: slight seams are possible, output is otherwise equivalent.
            4. Loader weights -> a lighter variant only if get_model_files() shows one on disk: UNETLoader weight_dtype = fp8_e4m3fn, or an fp8 checkpoint file with the same base model. Use UnetLoaderGGUF only if search_node_local() confirms the node exists and a .gguf file is present. Trade-off: fp8 / GGUF quantisation lowers quality slightly.
            5. --lowvram / --novram launch flags cannot be applied by you. If nothing above is enough, check argv from get_system_stats and tell the coordinator to call report_limitation with the exact edit: in run_nvidia_gpu.bat append --lowvram to the python main.py line, then restart ComfyUI.

            **dtype playbook** (cutlass_fp16_linear: K mismatch, expected scalar type, Half / BFloat16, mat1 and mat2 shapes):
            - Usually a mismatched model family or text encoder: an SD1.5 CLIP fed into an SDXL model, a wrong DualCLIPLoader.type (must match the UNET: flux, sdxl, sd3, wan, hunyuan_video ...), or fp8 weights on a node that expects fp16.
            - Check every loader with get_node_info() and get_model_files() and fix the parameter with update_workflow_parameter(); state which model family you assumed.
            - If the only fix is a different model file that is not on disk, tell the coordinator to call report_limitation with the file needed and where it goes.

            **shape playbook** (size mismatch, Sizes of tensors must match):
            - width / height on latent and image-resize nodes -> multiples of 64 (multiples of 8 at least), same aspect for every latent in the graph.
            - ControlNet, reference, mask and inpaint images must match the latent size: add or adjust the resize node feeding them, or set the latent to the image size.

            **Rules:**
            - Cheapest change first; do not stack every fix at once.
            - Every response lists the changes made and the quality trade-off of each.
            - Values passed to update_workflow_parameter are parsed as JSON: pass "1" for an int, "false" for a bool, plain text for names.
            - ALWAYS transfer back to ComfyUI-Debug-Coordinator; do not end without handoff.
            """,
            tools=[get_current_workflow, get_node_info, search_node_local, get_model_files, get_system_stats,
                   update_workflow_parameter, replace_node_class, update_workflow],
            handoffs=[agent],
            config={
                "max_tokens": 8192,
                **config
            }
        )

        agent.handoffs = [link_agent, workflow_bugfix_default_agent, parameter_agent, runtime_error_agent]

        # Initial message to start the debugging process
        messages = [{"role": "user", "content": f"Validate and debug this ComfyUI workflow."}]
            
        log.info(f"-- Starting workflow validation process for session {session_id}")

        result = Runner.run_streamed(
            agent,
            input=messages,
            max_turns=60,
        )
        log.info("=== Debug Coordinator starting ===")
        
        # Variables to track response state similar to mcp-client
        current_text = ''
        current_agent = "ComfyUI-Debug-Coordinator"
        last_yielded_length = 0
        
        # Collect debug events for final ext data
        debug_events = []
        
        # Collect workflow update ext data from tools
        workflow_update_ext = None

        # Outcome tracking: tool call id -> tool name, run_workflow results, reported limitation
        tool_names_by_call_id: Dict[str, str] = {}
        runs: List[Dict[str, Any]] = []
        limitation: Optional[Dict[str, Any]] = None

        async for event in result.stream_events():
            # Handle different event types according to OpenAI Agents documentation
            if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
                # Stream text deltas for real-time response
                delta_text = event.data.delta
                if delta_text:
                    current_text += delta_text
                    # Only yield text updates during streaming, similar to mcp-client
                    if len(current_text) > last_yielded_length:
                        last_yielded_length = len(current_text)
                        yield (current_text, None)
                
            elif event.type == "agent_updated_stream_event":
                new_agent_name = event.new_agent.name
                log.info(f"Handoff to: {new_agent_name}")
                current_agent = new_agent_name
                # Add handoff information to the stream
                if not current_text or current_text == '': 
                    handoff_text = f"▸ **Switching to {new_agent_name}**\n\n"
                else:
                    handoff_text = f"\n\n▸ **Switching to {new_agent_name}**\n\n"
                current_text += handoff_text
                last_yielded_length = len(current_text)
                
                # Collect debug event data
                debug_events.append({
                    "type": "agent_handoff",
                    "current_agent": current_agent,
                    "to_agent": new_agent_name,
                    "timestamp": len(current_text)
                })
                
                # Yield text update only
                yield (current_text, None)
                
            elif event.type == "run_item_stream_event":
                item_updated = False
                
                if event.item.type == "tool_call_item":
                    # Tool call started
                    tool_name = getattr(event.item.raw_item, 'name', 'unknown_tool')
                    call_id = getattr(event.item.raw_item, 'call_id', None)
                    if call_id:
                        tool_names_by_call_id[str(call_id)] = tool_name

                    log.info(f"-- Tool called: {tool_name}")
                    # Add tool call information
                    tool_text = f"\n\n⚙ *{current_agent} is using {tool_name}...*\n\n"
                    current_text += tool_text
                    item_updated = True
                    
                    # Collect debug event data
                    debug_events.append({
                        "type": "tool_call",
                        "tool": tool_name,
                        "agent": current_agent,
                        "timestamp": len(current_text)
                    })
                    
                elif event.item.type == "tool_call_output_item":
                    # Tool call result
                    output = str(event.item.output)
                    # Limit output length to avoid too long display
                    output_preview = output[:200] + "..." if len(output) > 200 else output
                    tool_result_text = f"\n\n● *Tool execution completed*\n\n```\n{output_preview}\n```\n\n"
                    current_text += tool_result_text
                    item_updated = True
                    
                    # Try to parse tool output and extract ext data
                    try:
                        tool_output_json = json.loads(output)
                        finished_tool = tool_names_by_call_id.get(str(getattr(event.item, 'call_id', None) or ''))
                        if isinstance(tool_output_json, dict):
                            if finished_tool == "run_workflow":
                                runs.append(tool_output_json)
                                log.info(f"-- run_workflow {tool_output_json.get('mode')}: {tool_output_json.get('status')}")
                            elif finished_tool == "report_limitation" and tool_output_json.get("limitation"):
                                limitation = tool_output_json["limitation"]
                                log.info(f"-- Limitation reported: {limitation.get('reason')}")
                        if isinstance(tool_output_json, dict) and tool_output_json.get("ext"):
                            for ext_item in tool_output_json["ext"]:
                                if ext_item.get("type") == "workflow_update" or ext_item.get("type") == "param_update":
                                    workflow_update_ext = ext_item
                                    log.info(f"-- Captured {ext_item.get('type')} ext from tool output, yielding immediately")
                                    
                                    # 立即yield workflow_update或param_update，让前端实时更新工作流
                                    ext_with_finished = {
                                        "data": [ext_item],
                                        "finished": False  # 标记为未完成，继续debug流程
                                    }
                                    yield (current_text, ext_with_finished)
                                    break
                    except (json.JSONDecodeError, TypeError):
                        # Tool output is not JSON, continue normally
                        pass
                    
                    # Collect debug event data
                    debug_events.append({
                        "type": "tool_result",
                        "output_preview": output_preview,
                        "agent": current_agent,
                        "timestamp": len(current_text)
                    })
                    
                elif event.item.type == "message_output_item":
                    # Message output completed
                    try:
                        message_content = ItemHelpers.text_message_output(event.item)
                        if message_content and message_content.strip():
                            # Avoid adding duplicate message content
                            if message_content not in current_text:
                                current_text += f"\n\n{message_content}\n\n"
                                item_updated = True
                                
                                # Collect debug event data
                                debug_events.append({
                                    "type": "message_complete",
                                    "content_length": len(message_content),
                                    "agent": current_agent,
                                    "timestamp": len(current_text)
                                })
                    except Exception as e:
                        log.error(f"Error processing message output: {str(e)}")
                
                # Update yielded length and yield text updates only
                if item_updated:
                    last_yielded_length = len(current_text)
                    yield (current_text, None)

        outcome = debug_outcome(runs, limitation)
        log.info(f"\n=== Debug process complete: outcome={outcome}, runs={len(runs)} ===")

        # Save final workflow checkpoint after debugging completion
        debug_completion_checkpoint_id = None
        try:
            current_workflow = get_workflow_data(session_id)
            if current_workflow:
                debug_completion_checkpoint_id = save_workflow_data(
                    session_id, 
                    current_workflow,
                    workflow_data_ui=None,  # UI format not available here
                    attributes={
                        "checkpoint_type": "debug_complete",
                        "description": "Workflow state after debug completion",
                        "action": "debug_complete",
                        "final_agent": current_agent
                    }
                )
                log.info(f"Debug completion checkpoint saved with ID: {debug_completion_checkpoint_id}")
        except Exception as checkpoint_error:
            log.error(f"Failed to save debug completion checkpoint: {checkpoint_error}")
        
        # Final yield with complete text and debug ext data, matching mcp-client format
        debug_ext = [{
            "type": "debug_complete",
            "data": {
                "status": "completed",
                "outcome": outcome,
                "runs": runs,
                "limitation": limitation,
                "final_agent": current_agent,
                "events": debug_events,
                "total_events": len(debug_events)
            }
        }]
        
        # Add debug checkpoint info if successful
        if debug_completion_checkpoint_id:
            debug_ext.append({
                "type": "debug_checkpoint",
                "data": {
                    "checkpoint_id": debug_completion_checkpoint_id,
                    "checkpoint_type": "debug_complete"
                }
            })
        
        # Include workflow_update ext if captured from tools
        final_ext = debug_ext
        if workflow_update_ext:
            final_ext = [workflow_update_ext] + debug_ext
            log.info(f"-- Including workflow_update ext in final response")
        
        # Return format matching mcp-client: {"data": ext, "finished": finished}
        ext_with_finished = {
            "data": final_ext,
            "finished": True
        }
        yield (current_text, ext_with_finished)
            
    except Exception as e:
        log.error(f"Error in debug_workflow_errors: {str(e)}")
        error_message = f"\n\n× Error occurred during debugging: {str(e)}\n\n"

        ext_with_finished = {
            "finished": True
        }
        yield (error_message, ext_with_finished)
