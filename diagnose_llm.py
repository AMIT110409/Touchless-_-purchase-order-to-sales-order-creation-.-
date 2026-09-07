
print("Starting diagnostic...", flush=True)
try:
    from langchain_community.llms import HuggingFacePipeline
    from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline
    import torch
    
    MODEL_ID = "Qwen/Qwen2.5-3B-Instruct" 
    DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {DEVICE}", flush=True)

    print(f"Loading tokenizer...", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    
    print(f"Loading model...", flush=True)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_ID,
        torch_dtype="auto",
        device_map="auto",
        trust_remote_code=True
    )
    
    print("Creating pipeline...", flush=True)
    pipe = pipeline(
        "text-generation",
        model=model,
        tokenizer=tokenizer,
        max_new_tokens=100,
        device_map="auto"
    )
    
    print("Creating LangChain wrapper...", flush=True)
    llm = HuggingFacePipeline(pipeline=pipe)
    print("SUCCESS: LLM Loaded.", flush=True)

except Exception as e:
    import traceback
    traceback.print_exc()
    print(f"FAILURE: {e}", flush=True)
