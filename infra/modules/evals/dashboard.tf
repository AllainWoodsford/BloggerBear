# -----------------------------------------------------------------------
# The Model Ranch and prompts dashboard, <prefix>-<env>-ranch-and-prompts (PR11b; section 5.3).
# Only while evals are on, like the rest of the module's active parts: with them off nothing is
# written to the evals log group, and a dashboard of empty panels would still cost its share of the
# account's dashboards (CloudWatch bills about US$3 a month for each past the first three).
#
# Every panel but one is a Logs Insights query over the evals log group, so each event's own fields
# (model, prompt version, reason, who emitted it) can be grouped any way without a custom metric
# per combination; they are billed per GB scanned each time the dashboard loads them, and the group
# holds a short JSON line per event. The other panel is prompt fallbacks: the PromptFallback metric
# the observability module's filters count from every pipeline Lambda's log (var.prompt_fallback_metric),
# which also covers a fallback that never reached the evals group.
#
# The events hold codes and ids only (common/evals.py), so nothing here can show free text.
# -----------------------------------------------------------------------

locals {
  # Every query reads the evals group, and only its events.
  evals_source = "SOURCE '${aws_cloudwatch_log_group.evals.name}' | filter schema = \"evals/1\""

  ranch_and_prompts_widgets = [
    {
      type   = "text"
      width  = 24
      height = 2
      properties = {
        markdown = "## Model Ranch and prompts (${var.environment_name})\nScored events from the evals log group as they arrive, before any rollup: a rating is the sum of the events' deltas. The rolled-up windows and gates are `admin_cli.py evals status`. Pipeline health is **${var.unique_name_prefix}-${var.environment_name}-pipeline-health**."
      }
    },
    {
      type   = "metric"
      width  = 12
      height = 6
      properties = {
        title   = "Prompt fallbacks per hour (every pipeline Lambda)"
        region  = var.aws_region
        view    = "timeSeries"
        period  = 3600
        yAxis   = { left = { min = 0, label = "per hour" } }
        metrics = [[var.prompt_fallback_metric.namespace, var.prompt_fallback_metric.name, { stat = "Sum", label = "fallbacks" }]]
      }
    },
    {
      type   = "log"
      width  = 12
      height = 6
      properties = {
        title  = "Events and rating per hour"
        region = var.aws_region
        view   = "timeSeries"
        query  = "${local.evals_source} | stats count(*) as events, sum(rating_delta) as rating by bin(1h)"
      }
    },
    {
      type   = "log"
      width  = 12
      height = 8
      properties = {
        title  = "Models: events and rating, lowest first"
        region = var.aws_region
        view   = "table"
        query  = "${local.evals_source} | filter ispresent(model_id) | stats count(*) as events, sum(rating_delta) as rating by model_id | sort rating asc | limit 50"
      }
    },
    {
      type   = "log"
      width  = 12
      height = 8
      properties = {
        title  = "Prompt versions: events and rating"
        region = var.aws_region
        view   = "table"
        query  = "${local.evals_source} | filter ispresent(prompt_id) | stats count(*) as events, sum(rating_delta) as rating by prompt_id, prompt_version | sort prompt_id asc | limit 50"
      }
    },
    {
      type   = "log"
      width  = 24
      height = 8
      properties = {
        title  = "Model x prompt cells, lowest first"
        region = var.aws_region
        view   = "table"
        query  = "${local.evals_source} | filter ispresent(model_id) and ispresent(prompt_id) | stats count(*) as events, sum(rating_delta) as rating by model_id, prompt_id, prompt_version | sort rating asc | limit 25"
      }
    },
    {
      type   = "log"
      width  = 12
      height = 6
      properties = {
        title  = "Events by reason and source"
        region = var.aws_region
        view   = "table"
        query  = "${local.evals_source} | stats count(*) as events, sum(rating_delta) as rating by reason, source | sort events desc | limit 50"
      }
    },
    {
      type   = "log"
      width  = 12
      height = 6
      properties = {
        title  = "Fallbacks recorded as events, by prompt version"
        region = var.aws_region
        view   = "table"
        query  = "${local.evals_source} | filter reason = \"Prompt Fallback\" | stats count(*) as fallbacks by prompt_id, prompt_version, source | sort fallbacks desc | limit 25"
      }
    },
    {
      type   = "log"
      width  = 24
      height = 6
      properties = {
        title  = "Latest bad events"
        region = var.aws_region
        view   = "table"
        query  = "${local.evals_source} | filter performance = \"bad\" | fields @timestamp, model_id, prompt_id, prompt_version, reason, rating_delta, source | sort @timestamp desc | limit 25"
      }
    },
  ]
}

resource "aws_cloudwatch_dashboard" "ranch_and_prompts" {
  count          = var.enabled ? 1 : 0
  dashboard_name = "${var.unique_name_prefix}-${var.environment_name}-ranch-and-prompts"

  # Opens on the last 7 days, as pipeline health does.
  dashboard_body = jsonencode({
    start          = "-P7D"
    periodOverride = "inherit"
    widgets        = local.ranch_and_prompts_widgets
  })
}
