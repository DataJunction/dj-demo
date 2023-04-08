from fastapi import FastAPI
import uvicorn

import openai
from openai.error import RateLimitError
import requests
from sentence_transformers import SentenceTransformer, util
import torch
import pandas as pd
import os
import sys

openai.api_key = os.environ["OPENAI_API_KEY"]
DJ_URL = os.environ["DJ_URL"]
  

print("READING METRIC DATA")
metrics_json = requests.get(
    f"{DJ_URL}/metrics",
).json()

metrics = pd.DataFrame(metrics_json)

print("LOADING METRIC QUERY MODEL")
MODEL = 'all-MiniLM-L6-v2'
embedder = SentenceTransformer(MODEL)

device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')


print("EMBEDDING DATA")
metric_embeddings = embedder.encode(metrics.description, convert_to_tensor=True)

def query_node(query: str, top_k = 5):
    """given a query for a dimension or metric name, queries for the nearest names 
    and returns the columns.
    
    Returns:
    {metric_or_dimension_name: [columns]}
    """
    top_k = min(top_k, len(metrics))

    query_embedding = embedder.encode(query, convert_to_tensor=True)

    cos_scores = util.cos_sim(query_embedding, metric_embeddings)[0]
    top_results = torch.topk(cos_scores, k=top_k)


    return pd.DataFrame([metrics.iloc[int(idx)] for idx in top_results[1]])

def make_request(utterance: str, max_retries: int = 5, model: str ='gpt-3.5-turbo') -> str:
    retries = 0  # Number of retries so far

    while retries < max_retries:
        try:
            completions = openai.ChatCompletion.create(model=model, temperature = 0, messages=[{"role": "user", "content": utterance}])
            return completions.choices[0]['message']['content']

        except RateLimitError:
            # Handle rate limiting error
            print(
                f"####### OpenAI rate limit reached, waiting for 1 minute #######",
            )
            time.sleep(60)  # Wait 1 minute before trying again
            retries += 1  # Increment the number of retries

    if retries == max_retries:
        print(
            f"Maximum number of retries exceeded. Giving up.",
        )
        sys.exit(1)
        

app = FastAPI()


@app.get("/query/{query}")
def query(query: str, n: int = 5, rel:float = 0):
    metric_query_prompt = f"""
    From the query '{query}', determine what the metric being calculated is. Reply only in the form: Metric: [the metric from the query]
    """.strip()

    metric_query = make_request(metric_query_prompt)

    metric_query = metric_query.replace("Metric:", "").strip()
    relevant_metrics = query_node(metric_query, n)
    metric_dimensions=("\n".join(relevant_metrics['name']+" ("+relevant_metrics['description']+"): "+relevant_metrics['dimensions'].astype(str)))
    request_prompt = f"""
    Your task is to give what columns to group by and what columns to filter by to answer the question '{query}'. 
    `dimensions` are the names of dimensiones comma-separated. 
    `filters` are comma-separated filters like in a sql query using dimension names e.g. `dimension.col=something`: 

    Here is a list of "metric name (metric description): [columns]":
    {metric_dimensions}

    You may choose only a single metric.
    Be sure to only use columns from the list for your chosen metric in the dimension and filter query parameters.

    Respond with only-

    Metric: your chosen metric name
    GroupBys: comma-separated values for your chosen columns
    Filters: comma-separated valid SQL filter expressions using your chosen columns
    """.strip()
    
    request_info = make_request(request_prompt)

    metric, dimensions, filters = request_info.split('\n')

    metric=metric.replace("Metric:", "").strip()
    dimensions=dimensions.replace("GroupBys:", "").strip()
    filters=filters.replace("Filters:", "").strip()
    
    request_url=f"{DJ_URL}/data/{metric}/?dimensions={dimensions}&filters={filters}"
    
    metrics_data = requests.get(request_url)
    return metrics_data.json()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8500)