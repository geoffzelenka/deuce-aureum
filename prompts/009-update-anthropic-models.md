In `report/generator.py`, update the model configuration to use 
different models for different phases of report generation.

Add to config.py / .env:
  SCAN_MODEL=claude-fable-5        # Phase 1 candidate identification
  RESEARCH_MODEL=claude-opus-4-8   # Phase 2 tool call loop  
  REPORT_MODEL=claude-fable-5      # Final report generation and ranking

In generate_report(), use the appropriate model constant for each 
API call:
  - scan_response: use SCAN_MODEL
  - research loop completions: use RESEARCH_MODEL  
  - final forced completion (after tool loop ends): use REPORT_MODEL

Also update the midmorning assessment in report/midmorning.py to 
use the same three-model routing.

Add a log line at report start:
  "Models: scan={SCAN_MODEL}, research={RESEARCH_MODEL}, 
   report={REPORT_MODEL}"

This allows easy A/B testing by swapping models in .env without 
touching code.
