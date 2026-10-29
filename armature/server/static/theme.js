// Runs in <head> before the stylesheet paints, so a stored theme never flashes.
(function applyStoredTheme() {
  var STORAGE_KEY = 'armature-theme';
  var CHOICES = ['system', 'light', 'dark'];

  function readChoice() {
    try {
      var stored = window.localStorage.getItem(STORAGE_KEY);
      return CHOICES.indexOf(stored) >= 0 ? stored : 'system';
    } catch (storageError) {
      return 'system';
    }
  }

  function apply(choice) {
    if (choice === 'light' || choice === 'dark') document.documentElement.setAttribute('data-theme', choice);
    else document.documentElement.removeAttribute('data-theme');
  }

  function save(choice) {
    try {
      window.localStorage.setItem(STORAGE_KEY, choice);
    } catch (storageError) {
      // A private window can refuse storage. The choice still applies to this page.
    }
  }

  apply(readChoice());

  function buildToggles() {
    var containers = document.querySelectorAll('[data-theme-toggle]');
    for (var containerIndex = 0; containerIndex < containers.length; containerIndex++) {
      var container = containers[containerIndex];
      if (container.firstChild) continue;
      var fieldset = document.createElement('fieldset');
      fieldset.className = 'theme-toggle';
      var legend = document.createElement('legend');
      legend.textContent = 'Appearance';
      fieldset.appendChild(legend);
      var groupName = 'theme-' + containerIndex;
      for (var choiceIndex = 0; choiceIndex < CHOICES.length; choiceIndex++) {
        var choice = CHOICES[choiceIndex];
        var label = document.createElement('label');
        var input = document.createElement('input');
        input.type = 'radio';
        input.name = groupName;
        input.value = choice;
        input.checked = readChoice() === choice;
        input.addEventListener('change', function onThemeChange(changeEvent) {
          apply(changeEvent.target.value);
          save(changeEvent.target.value);
          var sameChoice = document.querySelectorAll('.theme-toggle input[value="' + changeEvent.target.value + '"]');
          for (var inputIndex = 0; inputIndex < sameChoice.length; inputIndex++) sameChoice[inputIndex].checked = true;
        });
        var text = document.createElement('span');
        text.textContent = choice.charAt(0).toUpperCase() + choice.slice(1);
        label.appendChild(input);
        label.appendChild(text);
        fieldset.appendChild(label);
      }
      container.appendChild(fieldset);
    }
  }

  // The app shell adds its toggle containers after the session loads, so it calls this again.
  window.armatureThemeToggles = buildToggles;

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', buildToggles);
  else buildToggles();
})();
