Yes. For structured/tabular data, **natural-language-to-SQL** is often a much better fit than classic RAG over documents.

For your hypothetical stock database, the flow could be:

**User:** “What were the top 10 most traded stocks in 1999?”

→ LLM interprets intent  
→ LLM generates SQL  
→ Your application validates the SQL  
→ Database executes it  
→ Results are returned to the LLM  
→ LLM explains/formats the answer

For example, assuming a table like:

```text
stock_trades
-------------
ticker
trade_date
volume
close_price
exchange
```

the model might generate:

```sql
SELECT
    ticker,
    SUM(volume) AS total_volume
FROM stock_trades
WHERE trade_date >= '1999-01-01'
  AND trade_date < '2000-01-01'
GROUP BY ticker
ORDER BY total_volume DESC
LIMIT 10;
```

### You usually do NOT need to train the model

That would actually be fairly far down my list of approaches.

Modern models are already quite good at SQL. What matters more is giving the model enough information about **your database semantics**.

For example:

```text
TABLE stock_trades

ticker        TEXT    Stock ticker
trade_date    DATE    Trading date
volume        INTEGER Number of shares traded that day
close_price   FLOAT   Adjusted closing price
exchange      TEXT    NYSE, NASDAQ, etc.
```

Then give it rules like:

```text
"most traded" = highest SUM(volume)
"best performing" = percentage price appreciation
"1999" means 1999-01-01 through 1999-12-31
```

That is often enough.

---

## There are several levels you could use

I would think of the architecture roughly like this:

```text
                   ┌───────────────┐
                   │     User      │
                   │ Natural Lang. │
                   └───────┬───────┘
                           │
                           ▼
                   ┌───────────────┐
                   │      LLM      │
                   │ Intent/Query  │
                   └───────┬───────┘
                           │
             ┌─────────────┴─────────────┐
             │                           │
             ▼                           ▼
       SQL generation              Other tools
             │
             ▼
      ┌───────────────┐
      │ SQL Validator │
      └───────┬───────┘
              │
              ▼
       ┌─────────────┐
       │  Database   │
       │ SQL / DW    │
       └──────┬──────┘
              │
              ▼
        Result rows
              │
              ▼
       ┌──────────────┐
       │     LLM      │
       │ Explain /    │
       │ summarize    │
       └──────────────┘
```

### 1. LLM writes SQL directly

Simplest implementation.

You give the model the schema and ask:

> Generate a read-only SQL query that answers the user's question.

Works surprisingly well for modest databases.

Good for prototypes.

---

### 2. Tool-calling SQL agent

This is generally what I would prefer.

Don't merely tell the LLM:

> Write some SQL.

Give it tools resembling:

```text
get_database_schema()
execute_readonly_sql(sql)
```

The model can inspect the schema, create the query, execute it, examine the result, and potentially correct its query.

For example:

```text
User
  ↓
"What were the most traded stocks in 1999?"

Agent
  ↓
get_schema("stock_trades")

Database
  ↓
schema definition

Agent
  ↓
execute_sql("""
SELECT ticker, SUM(volume) ...
""")

Database
  ↓
AAPL   ...
MSFT   ...
...

Agent
  ↓
"Microsoft was #1..."
```

That's an **agent/tool architecture**, rather than simply a chatbot with RAG.

---

### 3. Semantic layer + SQL

This becomes particularly valuable in a business environment.

Instead of expecting the model to understand raw database fields such as:

```text
VOL_TOT
TDT
SEC_ID
ADJ_CLS_PRC
MKT_CD
```

you create a semantic layer:

```text
Metric: Trading Volume
Definition: Sum of daily share volume
SQL expression: SUM(VOL_TOT)

Dimension: Stock
Field: SEC_ID

Dimension: Year
Derived from: TDT
```

Then the LLM queries against those known business definitions.

This dramatically reduces ambiguity.

For an enterprise system, **this is often more important than model training**.

---

### 4. Controlled query templates

For especially high-accuracy environments, you don't even need the LLM to freely generate arbitrary SQL.

The model can convert:

> "Give me the top traded stocks in 1999."

into a structured request:

```json
{
  "metric": "trading_volume",
  "aggregation": "sum",
  "group_by": "ticker",
  "date_start": "1999-01-01",
  "date_end": "1999-12-31",
  "sort": "descending",
  "limit": 10
}
```

Your application generates the SQL deterministically.

That is:

```text
Natural language
       ↓
      LLM
       ↓
Structured query specification
       ↓
Deterministic SQL builder
       ↓
Database
```

I **really like this pattern** when correctness matters.

The LLM handles language.

Your software handles SQL.

---

## And then there actually is fine-tuning

You could fine-tune a model with examples such as:

```text
USER:
What were the 10 most heavily traded stocks in 1999?

ASSISTANT:
SELECT ticker, SUM(volume) ...
```

and thousands of similar examples.

That can improve behavior when:

- your schema is enormous,
- your terminology is unusual,
- queries follow recurring domain patterns,
- smaller/local models need better SQL performance,
- or you have tens of thousands of high-quality question→SQL examples.

But I would **not start there**.

I'd generally progress:

```text
1. Prompt + schema
        ↓
2. Tool-calling SQL agent
        ↓
3. Semantic definitions
        ↓
4. Structured query DSL
        ↓
5. Fine-tuning, if still necessary
```

There's an important distinction here from the document/RAG systems we've discussed before. If your information is inherently relational—stocks, parts, BOMs, maintenance records, inventory, financial transactions, schedules, etc.—**don't turn everything into embeddings and hope retrieval solves it**. SQL/database retrieval should remain the authoritative computation layer.

You can still combine the approaches:

```text
                 User
                   │
                   ▼
                  LLM
             ┌─────┴─────┐
             │           │
             ▼           ▼
           SQL           RAG
       structured     documents
          data        manuals/
             │         policies
             │           │
             └─────┬─────┘
                   ▼
                  LLM
                   │
                   ▼
                 Answer
```

That's often what a serious enterprise AI system ultimately looks like: **LLM as the reasoning/orchestration interface, SQL for structured truth, and RAG for unstructured knowledge.**

---

# Fine Tuning

For stock data, you normally would **not fine-tune the model on the stock rows themselves**. You would fine-tune it on the behavior you want—usually translating natural-language questions into the correct SQL or structured query.

Suppose your database looks like:

```sql
CREATE TABLE daily_stock (
    ticker TEXT,
    trade_date DATE,
    open_price REAL,
    close_price REAL,
    volume BIGINT,
    market_cap REAL
);
```

Your fine-tuning data could contain many examples like this:

```json
{
  "messages": [
    {
      "role": "user",
      "content": "What were the top 10 most traded stocks in 1999?"
    },
    {
      "role": "assistant",
      "content": "SELECT ticker, SUM(volume) AS total_volume FROM daily_stock WHERE trade_date >= '1999-01-01' AND trade_date < '2000-01-01' GROUP BY ticker ORDER BY total_volume DESC LIMIT 10;"
    }
  ]
}
```

Then another:

```json
{
  "messages": [
    {
      "role": "user",
      "content": "Which stocks gained the most during 2001?"
    },
    {
      "role": "assistant",
      "content": "..."
    }
  ]
}
```

And another:

```json
{
  "messages": [
    {
      "role": "user",
      "content": "Show average daily volume by month for MSFT in 2005."
    },
    {
      "role": "assistant",
      "content": "..."
    }
  ]
}
```

The important idea is:

**You train on question → desired behavior**, not database contents.

You might build thousands of examples covering things like date filtering, aggregation, grouping, joins, rankings, percentage changes, moving averages, market-cap filters, sectors, exchanges, aliases, and ambiguous wording.

For example, teach the model that:

```text
"most traded"
"highest trading activity"
"most heavily traded"
"largest share volume"
```

all map to something like:

```sql
SUM(volume)
```

That domain vocabulary is where fine-tuning can become useful.

You could also train it to emit a safer intermediate representation instead of SQL. For example:

```json
{
  "metric": "volume",
  "aggregation": "sum",
  "group_by": ["ticker"],
  "date_range": {
    "start": "1999-01-01",
    "end": "1999-12-31"
  },
  "sort": {
    "field": "volume",
    "direction": "desc"
  },
  "limit": 10
}
```

Then your own code converts that into SQL.

For enterprise systems, I often prefer this because you can validate the structure before touching the database.

A practical fine-tuning pipeline would look like:

```text
Database schema
      +
Business definitions
      +
Real user questions
      ↓
Create question → correct query examples
      ↓
Training set
      ↓
Fine-tune base LLM
      ↓
Evaluate on questions it never saw
      ↓
Deploy behind SQL validation
```

You'd normally split the examples into roughly three groups: training examples, validation examples, and a completely separate test set. The test set matters a lot because otherwise you can convince yourself the fine-tuning worked when the model merely memorized patterns.

For instance, if training contains:

> Top 10 stocks by volume in 1999

your test should ask something materially different like:

> Which five securities had the greatest aggregate share turnover between January 1, 2003 and June 30, 2003?

The model needs to generalize.

One especially important point: **fine-tuning does not keep the stock database current**.

If Apple gets another 10 million trading records tomorrow, you do not retrain the model.

The database remains external:

```text
Fine-tuned model
      │
      │ generates query
      ▼
Live Stock Database
      │
      ▼
Current Results
```

The model learns **how to ask the database**, while the database contains the facts.

That distinction is fundamental.

If you had 20 years of stock data containing billions of rows, trying to "teach the model all of that data" through fine-tuning would generally be the wrong architecture. SQL is vastly better at:

```sql
SUM()
AVG()
COUNT()
MAX()
MIN()
GROUP BY
ORDER BY
JOIN
WHERE
```

The LLM's job is translating a messy human request into those operations.

For example:

```text
User:
"What tech stocks really took off right after the dot-com crash,
but only show companies that were actually trading pretty heavily?"

        ↓

Fine-tuned LLM

        ↓

{
  sector: "Technology",
  period: "2002-2004",
  metric: "price_return",
  minimum_average_volume: ...
}

        ↓

SQL generator

        ↓

Database
```

That's where fine-tuning can really earn its keep: teaching the model your organization's vocabulary and intent.

And you don't necessarily need massive datasets. A few hundred **excellent and diverse examples** can sometimes materially improve a smaller model, while several thousand carefully constructed examples can make it much more domain-specific. Quality and coverage are generally more important than simply generating millions of near-duplicate SQL examples.

For your hypothetical, I would probably build the first version **without fine-tuning**, collect actual user questions plus the corrected SQL, and let that naturally become your future fine-tuning dataset. That gives you training examples based on what people really ask rather than what you guessed they would ask.

---

# Fine Tuning the Model

Yes. If you're fine-tuning an open-weight model from Hugging Face, the basic recipe is pretty straightforward.

You need: an open model, a training environment with a GPU, a fine-tuning framework, your dataset, and a way to evaluate/save the result.

A typical flow is:

1. Pick a base model from Hugging Face, such as a Qwen, Llama-compatible, Mistral, or DeepSeek-family model that fits your hardware and license needs.
2. Download the model and tokenizer.
3. Format your training data into instruction/chat examples.
4. Fine-tune using either full fine-tuning or, much more commonly, **LoRA/QLoRA**.
5. Save the adapter or merged model.
6. Run evaluation on held-out questions.
7. Deploy it with something like vLLM, Transformers, Ollama, llama.cpp, or another inference server.

For most practical projects, I would start with **QLoRA**, not full fine-tuning. QLoRA keeps the base model mostly frozen and trains small adapter weights, which dramatically reduces VRAM and compute requirements.

The software stack commonly looks like:

```text
Hugging Face Transformers
        +
Datasets
        +
PEFT          ← LoRA / QLoRA
        +
TRL           ← supervised fine-tuning utilities
        +
bitsandbytes  ← 4-bit quantization
        +
PyTorch
```

Conceptually:

```text
Hugging Face model
       ↓
Load in 4-bit
       ↓
Attach LoRA adapters
       ↓
Train on your examples
       ↓
Save LoRA adapter
       ↓
Base model + adapter
       ↓
Fine-tuned model
```

For example, your final files may be surprisingly small:

```text
Qwen base model:       15 GB
Your LoRA adapter:    200–800 MB
```

You don't necessarily create another complete 15 GB model. At inference time you can load:

```text
Base Qwen model
+
your SQL adapter
```

or merge the adapter into the base model afterward.

For your stock SQL example, the setup might be:

```text
Base model:
Qwen-family instruct model

Training:
~2,000 question → SQL examples

Method:
QLoRA

GPU:
1–4 NVIDIA GPUs

Libraries:
PyTorch
Transformers
PEFT
TRL
bitsandbytes
```

Then you'd run training with parameters such as:

```text
learning rate
number of epochs
batch size
LoRA rank
LoRA alpha
context length
```

You also need enough disk space for the base model, checkpoints, and training data.

The hardware requirement depends heavily on model size. Roughly, with QLoRA:

| Model | Practical GPU starting point |
|---|---|
| 3B–4B | ~8–12 GB VRAM |
| 7B–8B | ~16–24 GB |
| 14B | ~24–48 GB |
| 32B | ~48–80+ GB |
| 70B | multiple high-memory GPUs |

So with something like an H100 server, this becomes very comfortable. With a home RTX 4060, you'd usually stick to roughly a 3B-class model or use aggressive quantization.

One other thing matters almost as much as training: **evaluation**. Before fine-tuning, run maybe 200 held-out SQL questions against the base model. Then run the exact same benchmark after fine-tuning. For SQL, you ideally don't just compare text—you actually execute the generated SQL against a test database and determine whether it produced the correct result.

So your complete project is really:

```text
1. Base model
2. Training dataset
3. Fine-tuning software
4. GPU compute
5. Training configuration
6. Validation/test dataset
7. Evaluation harness
8. Saved adapter/model
9. Inference/deployment environment
```

For a first experiment, something like **Qwen 7B/8B-class + QLoRA + TRL/PEFT** is a very reasonable way to learn the process.

---

# Book Example

Yes — something like this absolutely could be used. What the book is showing is a different approach from NL→SQL: **turn each row of tabular data into a hybrid vector representation, then retrieve “similar” rows with vector search.**

In the example, it takes three kinds of fields and combines them:

```text
text fields
name + description + category
        ↓
text embedding

numeric fields
price, rating, etc.
        ↓
normalized numeric vector

boolean fields
in_stock, etc.
        ↓
0 / 1 values

then concatenate:

[text embedding | normalized numbers | booleans]
                ↓
        hybrid vector
                ↓
          vector database
```

So for a product search like:

> “Best notebook for gaming and work”

this can work quite well, because that is fundamentally a **similarity/recommendation** problem.

For stock data, imagine rows like:

```text
ticker
company_name
sector
description
price
market_cap
avg_volume
pe_ratio
dividend_yield
```

You might build a vector like:

```text
embedding("Microsoft Technology software cloud computing")
+
normalized(market_cap)
+
normalized(avg_volume)
+
normalized(pe_ratio)
+
normalized(dividend_yield)
```

Then a request like:

> “Find me large technology companies with high trading volume that are similar to Microsoft”

could be a very reasonable use case for this approach.

But there is a big distinction from your earlier example.

If the user asks:

> “What were the top 10 most traded stocks in 1999?”

I would **not** use this vector technique as the primary solution.

That question has an exact mathematical answer:

```sql
SELECT ticker, SUM(volume)
FROM trades
WHERE year = 1999
GROUP BY ticker
ORDER BY SUM(volume) DESC
LIMIT 10;
```

Vector similarity is approximate. SQL is exact.

The book's approach is strongest for questions like:

> “Find products similar to a high-end gaming laptop.”

> “Find stocks resembling mature dividend-paying technology companies.”

> “Which companies look most similar to Nvidia based on business description, size, valuation and trading activity?”

Those are fuzzy similarity questions.

SQL is strongest for:

> “Top 10 by volume.”

> “Average return from 1999–2005.”

> “Companies with P/E < 15 and market cap > $10B.”

> “Count stocks by sector.”

> “Which company had the highest percentage increase?”

So for a serious chat-over-tabular-data system, I would actually combine them:

```text
                    User
                     │
                     ▼
                    LLM
                     │
             determine intent
               /           \
              /             \
             ▼               ▼
      exact/analytical    similarity/
          question        semantic question
             │               │
             ▼               ▼
            SQL          Vector Search
             │               │
             └───────┬───────┘
                     ▼
                    LLM
                     │
                     ▼
                   Answer
```

And you can go one level further. A question like:

> “Find the 20 stocks most similar to Nvidia and tell me which five had the highest return in 2025.”

could use **both**:

```text
"Nvidia-like stocks"
        ↓
vector retrieval
        ↓
20 candidate tickers
        ↓
SQL
        ↓
calculate actual 2025 return
        ↓
sort and return top 5
```

That's a very powerful pattern.

There is one part of the book example I'd be cautious about, though: simply concatenating text embeddings with normalized numeric values is conceptually easy, but it can be crude. The relative scaling of the text vector versus the numerical dimensions can strongly affect similarity. In production, I'd often prefer separate semantic retrieval plus structured filtering/ranking, or explicitly weighted hybrid scoring, rather than assuming concatenation gives the correct notion of “similarity.”

So your intuition is right: **yes, tabular data can absolutely be embedded and retrieved with RAG/vector techniques.** It just doesn't replace SQL. A good rule is:

**SQL answers “which rows satisfy/calculate this exactly?”**

**Vector retrieval answers “which rows are semantically or behaviorally similar to what I mean?”**

For the kind of enterprise systems you've been thinking about, I'd probably design the agent so it can choose among **SQL, vector search, or both**, rather than committing the entire tabular dataset to one retrieval method.

# When to use what

Exactly. The important thing is not that one method is “better”—they solve **different kinds of questions**.

For structured/tabular data, I'd think about four main approaches:

| Method | Best when the user wants… | Poor choice when… |
|---|---|---|
| **SQL / NL→SQL** | Exact answers, calculations, filtering, grouping, ranking | The request is fuzzy/semantic |
| **Vector/embedding retrieval** | Similarity, meaning, recommendations | The answer requires exact math/ranking |
| **Traditional RAG** | Information contained in documents/text | The truth is already structured in tables |
| **Fine-tuned model** | Better interpretation/query generation for a specialized domain | You're trying to teach it changing factual data |

### SQL — exact questions

Use SQL when the question can ultimately be expressed as database operations.

> “What were the 10 most traded stocks in 1999?”

> “What was Microsoft's average closing price in 2004?”

> “Show stocks with market cap > $10B and P/E < 15.”

> “Which sector had the highest average return?”

These are fundamentally:

```text
filter
aggregate
join
sort
count
calculate
```

**Use the LLM to understand the question; let the database calculate the answer.**

This would generally be my default for structured business data.

---

### Embeddings/vector search — fuzzy questions

The book example you showed is useful when **similarity itself is the problem**.

Suppose your stock table contains:

```text
Ticker
Company description
Sector
Market cap
P/E
Revenue growth
Volatility
Dividend yield
...
```

Now someone asks:

> “Find companies similar to Microsoft.”

There's no objectively correct SQL definition of `similar`.

That's where embeddings/hybrid vectors become interesting.

Likewise:

> “Find mature technology companies resembling Microsoft but with stronger dividends.”

Now you're combining semantic meaning with numerical characteristics.

Vector/hybrid retrieval can make sense.

But don't use vector similarity to answer:

> “Which 10 stocks had the highest volume in 1999?”

You already possess the exact numbers. Approximate nearest-neighbor search is unnecessary and can give the wrong answer.

---

### Traditional RAG — unstructured knowledge

Now imagine you have:

```text
SEC filings
Annual reports
Analyst reports
News articles
Earnings transcripts
Company descriptions
```

and someone asks:

> “Why did investors become concerned about Microsoft's cloud growth?”

That's a document retrieval problem.

```text
Question
   ↓
embedding search
   ↓
relevant document chunks
   ↓
LLM
   ↓
answer
```

Trying to solve that purely with SQL would be awkward.

---

### Fine-tuning — teach behavior, not facts

This is the one that's easiest to confuse with the others.

Suppose users at your company use specialized terminology:

> “Give me the biggest movers after the dot-com washout.”

And your system repeatedly misunderstands what users mean.

You could fine-tune an open-weight model with examples teaching it:

```text
User language
      ↓
correct interpretation
      ↓
SQL / tool call
```

Fine-tuning changes **how the model behaves**.

It generally shouldn't become the database.

If tomorrow's stock prices change, you update the database—not retrain the model.

---

The really interesting systems therefore combine all of these:

```text
                         USER
                           │
                           ▼
                          LLM
                    understands intent
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
           SQL         Vector Search     RAG
            │              │              │
       exact data      similarity      documents
            │              │              │
            └──────────────┼──────────────┘
                           ▼
                          LLM
                           │
                           ▼
                         ANSWER
```

And potentially the **LLM itself has been fine-tuned** to become better at choosing and using those tools.

A useful mental test is:

**“What are the top 10?” → SQL**

**“What is most similar?” → vectors**

**“What do the documents say?” → RAG**

**“My model consistently misunderstands how my users talk or how I want it to behave.” → consider fine-tuning**

And combinations are completely legitimate. For example:

> “Find companies whose business is similar to Nvidia, then rank those companies by revenue growth over the last five years, and explain the top three using their annual reports.”

That single question could legitimately require **vector search → SQL → RAG → LLM synthesis**.

That's much closer to how I'd think about building a robust data agent than trying to make one retrieval technique solve every problem.

---

# Fine Tuning Continued

I would show fine-tuning as something that improves the **LLM itself**, rather than as another retrieval path alongside SQL and vector search.

```text
                     ┌──────────────────────┐
                     │   Fine-Tuned LLM     │
                     │     (optional)       │
                     │                      │
                     │ Learns domain terms, │
                     │ intent, tool use,    │
                     │ SQL patterns, etc.   │
                     └──────────┬───────────┘
                                │
                                ▼
                              User
                                │
                                ▼
                               LLM
                     ┌─────────────────────┐
                     │  Determine Intent   │
                     └──────────┬──────────┘
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                 │
              ▼                 ▼                 ▼
       Exact / Analytical   Similarity /       Unstructured
           Question          Semantic            Knowledge
              │              Question             Question
              │                 │                 │
              ▼                 ▼                 ▼
         Generate SQL      Vector Search           RAG
              │                 │             Document Search
              ▼                 │                 │
          Database              │                 │
              │                 │                 │
              └─────────────────┼─────────────────┘
                                │
                                ▼
                               LLM
                     Interpret / Synthesize
                                │
                                ▼
                              Answer
```

But I'd add one more important possibility: **fine-tuning can improve multiple stages of this pipeline.**

```text
                       TRAINING DATA
                            │
          ┌─────────────────┼──────────────────┐
          │                 │                  │
   User → Intent       User → SQL       User → Tool Choice
      examples           examples            examples
          │                 │                  │
          └─────────────────┼──────────────────┘
                            │
                            ▼
                    ┌───────────────┐
                    │  Fine-Tuning  │
                    └───────┬───────┘
                            │
                            ▼
                     Domain-Tuned LLM
                            │
                            ▼
                           User
                            │
                            ▼
                    Understand Request
                            │
              ┌─────────────┼─────────────┐
              │             │             │
              ▼             ▼             ▼
             SQL         Vectors          RAG
          exact data     similarity     documents
              │             │             │
              └─────────────┼─────────────┘
                            │
                            ▼
                           LLM
                            │
                            ▼
                          Answer
```

For the hypothetical stock system, you could therefore fine-tune for several different reasons.

**Intent recognition:** Teach it that “biggest movers,” “most heavily traded,” “companies like Nvidia,” etc. mean different things and require different tools.

**SQL generation:** Train examples of natural-language stock questions paired with correct SQL.

**Tool selection:** Teach it that “top 10 by volume” should call SQL, while “companies similar to Nvidia” should use vector search.

**Domain semantics:** Teach it specialized definitions—for example, exactly what your organization means by “return,” “trading volume,” “large cap,” etc.

**Response behavior:** Teach it how results should be interpreted and presented.

The critical separation I'd maintain is:

```text
Fine-tuning = teach the LLM HOW to behave

SQL          = retrieve/calculate exact structured facts

Vector search = retrieve based on similarity/meaning

RAG           = retrieve relevant unstructured knowledge
```

So **fine-tuning doesn't really compete with SQL, vector search, or RAG**. It can sit above all three and make the model much better at deciding **when and how to use each one**.