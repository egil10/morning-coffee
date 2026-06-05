// Morning Coffee — tiny client script: theme toggle + quiz interactivity.
(function () {
  // ---- theme ----
  var root = document.documentElement;
  try {
    var saved = localStorage.getItem("mc-theme");
    if (saved) root.setAttribute("data-theme", saved);
  } catch (e) {}

  var toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var sysDark = window.matchMedia &&
        window.matchMedia("(prefers-color-scheme: dark)").matches;
      var current = root.getAttribute("data-theme") || (sysDark ? "dark" : "light");
      var next = current === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("mc-theme", next); } catch (e) {}
    });
  }

  // ---- quiz ----
  var questions = Array.prototype.slice.call(document.querySelectorAll(".q"));
  if (!questions.length) return;

  var answered = 0, correct = 0;
  var score = document.getElementById("score");

  function render() {
    if (!score) return;
    if (answered === 0) {
      score.innerHTML = "pick an answer to begin";
    } else {
      score.innerHTML = "answered <b>" + answered + "/" + questions.length +
        "</b> · <b>" + correct + "</b> correct";
    }
  }
  render();

  questions.forEach(function (q) {
    var opts = Array.prototype.slice.call(q.querySelectorAll(".opt"));
    opts.forEach(function (opt) {
      opt.addEventListener("click", function () {
        if (q.getAttribute("data-answered") === "true") return;
        q.setAttribute("data-answered", "true");
        var isCorrect = opt.getAttribute("data-correct") === "true";
        opts.forEach(function (o) {
          if (o.getAttribute("data-correct") === "true") {
            o.classList.add("correct");
            o.insertAdjacentHTML("beforeend", '<span class="mark">&#10003;</span>');
          }
        });
        if (!isCorrect) {
          opt.classList.add("wrong");
          opt.insertAdjacentHTML("beforeend", '<span class="mark">&#10007;</span>');
        }
        answered += 1;
        if (isCorrect) correct += 1;
        render();
      });
    });
  });
})();
