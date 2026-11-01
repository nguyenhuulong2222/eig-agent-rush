// Kanaloa County Elections (FICTIONAL DEMO): renders results.json.
// DOM is built with textContent only, never innerHTML, so data cannot inject markup.
"use strict";

(function () {
  var RESULTS_URL = "data/results.json";
  var fmtInt = new Intl.NumberFormat("en-US");
  var fmtPct = new Intl.NumberFormat("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  var fmtTime = new Intl.DateTimeFormat("en-US", {
    weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit", timeZone: "Pacific/Honolulu", timeZoneName: "short"
  });

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    }
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function pct(part, whole) {
    return whole > 0 ? (part * 100) / whole : 0;
  }

  function setText(id, text) {
    var node = document.getElementById(id);
    if (node) node.textContent = text;
  }

  function updateLabel(data) {
    var when = fmtTime.format(new Date(data.update.published_at));
    return "Update " + data.update.sequence + ", " + when;
  }

  function renderHome(data) {
    setText("status-update", updateLabel(data));
    setText("status-ballots", fmtInt.format(data.turnout.ballots_cast));
    setText("status-turnout", fmtPct.format(pct(data.turnout.ballots_cast, data.turnout.registered_voters)) + "%");
  }

  function renderContest(contest) {
    var section = el("section", { "class": "contest", "aria-labelledby": "c-" + contest.id });
    var head = el("div", { "class": "contest-head" });
    head.appendChild(el("h2", { id: "c-" + contest.id }, contest.title));
    head.appendChild(el("p", { "class": "reporting" },
      contest.precincts_reporting + " of " + contest.precincts_total + " precincts reporting"));
    section.appendChild(head);

    var meter = el("div", { "class": "reporting-meter", "aria-hidden": "true" });
    var fill = el("span");
    fill.style.width = pct(contest.precincts_reporting, contest.precincts_total).toFixed(1) + "%";
    meter.appendChild(fill);
    section.appendChild(meter);

    var sorted = contest.candidates.slice().sort(function (a, b) { return b.votes - a.votes; });
    var leader = sorted.length ? sorted[0].votes : 0;

    var table = el("table", { "class": "tally" });
    table.appendChild(el("caption", null,
      contest.type === "measure" ? "Votes on this measure" : "Votes for each candidate"));
    var thead = el("thead");
    var hr = el("tr");
    hr.appendChild(el("th", { scope: "col" }, contest.type === "measure" ? "Choice" : "Candidate"));
    hr.appendChild(el("th", { scope: "col", "class": "num" }, "Share and votes"));
    thead.appendChild(hr);
    table.appendChild(thead);

    var tbody = el("tbody");
    sorted.forEach(function (c) {
      var share = pct(c.votes, contest.total_votes);
      var row = el("tr", c.votes === leader && leader > 0 ? { "class": "leading" } : null);
      var name = el("th", { scope: "row" }, c.name);
      var bar = el("span", { "class": "bar", "aria-hidden": "true" });
      var barFill = el("span");
      barFill.style.width = share.toFixed(1) + "%";
      bar.appendChild(barFill);
      name.appendChild(bar);
      row.appendChild(name);

      var num = el("td", { "class": "num" });
      num.appendChild(el("span", { "class": "share" }, fmtPct.format(share) + "%"));
      num.appendChild(el("span", { "class": "votes" }, fmtInt.format(c.votes) + " votes"));
      row.appendChild(num);
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    section.appendChild(table);

    section.appendChild(el("p", { "class": "contest-foot" },
      "Total votes in this contest: " + fmtInt.format(contest.total_votes)));
    return section;
  }

  function renderResults(data) {
    var meta = document.getElementById("results-meta");
    meta.textContent = "";
    meta.appendChild(el("strong", null, updateLabel(data)));
    meta.appendChild(document.createTextNode(
      ". " + fmtInt.format(data.turnout.ballots_cast) + " ballots counted of " +
      fmtInt.format(data.turnout.registered_voters) + " registered voters (" +
      fmtPct.format(pct(data.turnout.ballots_cast, data.turnout.registered_voters)) + "% turnout). " +
      "Status: " + data.update.status + "."));

    var host = document.getElementById("contests");
    host.textContent = "";
    data.contests.forEach(function (contest) { host.appendChild(renderContest(contest)); });
  }

  function showError(page) {
    var message = "Results could not be loaded. Refresh the page, or try again in a few minutes.";
    if (page === "results") {
      var meta = document.getElementById("results-meta");
      meta.textContent = message;
      meta.className = "notice";
    } else {
      ["status-update", "status-ballots", "status-turnout"].forEach(function (id) { setText(id, "Unavailable"); });
    }
  }

  var page = document.body.getAttribute("data-page");
  if (page !== "home" && page !== "results") return;

  fetch(RESULTS_URL, { cache: "no-store" })
    .then(function (res) {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.json();
    })
    .then(function (data) {
      if (page === "home") renderHome(data);
      else renderResults(data);
    })
    .catch(function () { showError(page); });
})();
