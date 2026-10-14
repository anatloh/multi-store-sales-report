# Multi-Store Sales Reporting Pipeline

An automated ETL data pipeline and reporting system designed to extract retail sales data, process item-level breakdowns via Python and Playwright, store records in SQL Server, and orchestrate automated schedules through n8n.

## Tech Stack
* **Language:** Python (Pandas, Playwright, PyODBC)[cite: 22]
* **Database:** Microsoft SQL Server[cite: 22]
* **Orchestration:** n8n Workflow Automation[cite: 22]
* **Messaging:** LINE API Integration[cite: 22]

## Project Structure

```text
├── src/
│   └── api_etl.py         # Main Python ETL extraction and processing script
├── .env.example           # Template for environment variables and DB credentials
├── .gitignore             # Files and directories to ignore (e.g., .env, __pycache__)
├── requirements.txt       # Python dependencies (pandas, playwright, pyodbc, etc.)
└── README.md              # Project documentation
