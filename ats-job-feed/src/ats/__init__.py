"""ATS adapters. Everything here is importable and usable without Apify."""

from . import ashby, greenhouse, lever, workday
from .filters import JobFilter
from .http import RateLimitedClient
from .models import AtsError, CompanyRef, HttpStatusError, Job, RunReport
from .refs import parse_company

ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "workday": workday,
}

__all__ = [
    "ADAPTERS", "AtsError", "CompanyRef", "HttpStatusError", "Job", "JobFilter",
    "RateLimitedClient", "RunReport", "parse_company",
]
