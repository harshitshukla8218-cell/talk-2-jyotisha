(function () {
  "use strict";

  let csrfToken = "";
  let authenticatedUser = null;
  const form = document.getElementById("authForm");
  const accountModal = document.getElementById("accountModal");
  const errorBox = document.getElementById("authError");
  const submitButton = document.getElementById("authSubmitBtn");
  const passwordInput = document.getElementById("authPassword");
  const nameInput = document.getElementById("authName");
  const nameField = document.getElementById("signupNameField");
  const nextPath = new URLSearchParams(window.location.search).get("next");
  const protectedPages = new Set([
    "/astro-essentials.html",
    "/birth-details.html",
    "/exclusive-yantra.html",
    "/kundli-report.html",
    "/premium-gemstone.html",
    "/rashi-yantra.html",
    "/standard-gemstone.html"
  ]);

  try {
    localStorage.removeItem("t2j_users");
    localStorage.removeItem("t2j_session");
  } catch (error) {
    if (!(error instanceof DOMException)) {
      throw error;
    }
    console.warn("Legacy local account data could not be cleared from this browser.");
  }

  function showError(message) {
    if (errorBox) {
      errorBox.textContent = message;
      errorBox.style.display = "block";
      errorBox.setAttribute("role", "alert");
    }
  }

  function updateNavAccountLink() {
    const link = document.getElementById("navAccountLink");
    const signupLink = document.getElementById("navSignupLink");
    document.querySelectorAll("[data-auth-required]").forEach(function (item) {
      item.hidden = !authenticatedUser;
    });
    if (signupLink) {
      signupLink.hidden = Boolean(authenticatedUser);
    }
    if (!link) {
      return;
    }
    if (authenticatedUser) {
      link.textContent = "Logout";
      link.href = "#";
      link.onclick = function (event) {
        event.preventDefault();
        window.logout();
      };
    } else {
      link.textContent = "Login";
      link.href = "#";
      link.onclick = function (event) {
        event.preventDefault();
        window.openAccountModal();
      };
    }
  }

  function setAuthenticatedUser(user) {
    authenticatedUser = user;
    window.authUser = user;
    // The marketing homepage is public. Authentication is only required
    // by the protected member routes enforced by the Flask backend.
    updateNavAccountLink();
  }

  async function requestJson(path, options) {
    const requestOptions = Object.assign({ credentials: "same-origin" }, options || {});
    requestOptions.headers = Object.assign(
      { Accept: "application/json" },
      requestOptions.headers || {}
    );
    if (requestOptions.body && typeof requestOptions.body !== "string") {
      requestOptions.headers["Content-Type"] = "application/json";
      requestOptions.body = JSON.stringify(requestOptions.body);
    }
    if (requestOptions.method && requestOptions.method !== "GET" && csrfToken) {
      requestOptions.headers["X-CSRFToken"] = csrfToken;
    }

    const response = await fetch(path, requestOptions);
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.toLowerCase().includes("application/json")) {
      const error = new Error(
        "The server returned an unexpected page instead of API data (HTTP " +
          response.status +
          "). Restart the dashboard server and reload this page."
      );
      error.status = response.status;
      throw error;
    }
    let payload;
    try {
      payload = await response.json();
    } catch (cause) {
      const error = new Error("The server returned an invalid API response.");
      error.status = response.status;
      error.cause = cause;
      throw error;
    }
    if (payload.csrfToken) {
      csrfToken = payload.csrfToken;
      window.csrfToken = csrfToken;
    }
    if (!response.ok) {
      const error = new Error(payload.error || "The request could not be completed.");
      error.status = response.status;
      if (
        response.status === 401 &&
        path !== "/api/login" &&
        protectedPages.has(window.location.pathname)
      ) {
        const returnPath =
          window.location.pathname + window.location.search + window.location.hash;
        window.location.replace(
          "/index.html?auth=required&next=" + encodeURIComponent(returnPath)
        );
      }
      throw error;
    }
    return payload;
  }

  window.apiRequest = requestJson;
  window.getSession = function () {
    return authenticatedUser ? authenticatedUser.email : null;
  };
  window.getUsers = function () {
    return authenticatedUser ? [authenticatedUser] : [];
  };
  window.updateNavAccountLink = updateNavAccountLink;
  window.showAuthError = showError;
  window.openAccountModal = function (mode) {
    if (typeof window.switchAuthTab === "function") {
      window.switchAuthTab(mode || "login");
    }
    if (typeof window.openModal === "function") {
      window.openModal("accountModal");
    }
    const firstField = mode === "signup" && nameInput
      ? nameInput
      : document.getElementById("authEmail");
    if (accountModal && typeof accountModal.scrollIntoView === "function") {
      accountModal.scrollIntoView({ block: "center" });
    }
    if (firstField) {
      firstField.focus({ preventScroll: true });
    }
  };
  window.logout = async function () {
    try {
      await requestJson("/api/logout", { method: "POST" });
      setAuthenticatedUser(null);
      window.location.replace("/index.html");
    } catch (error) {
      showError(error.message);
    }
  };

  window.authReady = requestJson("/api/me")
    .then(function (result) {
      setAuthenticatedUser(result.user);
      return result.user;
    })
    .catch(function (error) {
      setAuthenticatedUser(null);
      console.error("Could not verify the current account session.", error);
      return null;
    });

  window.requireAccountForBooking = async function (event, serviceName) {
    if (event) {
      event.preventDefault();
    }

    const user = await window.authReady;
    if (user) {
      if (serviceName) {
        document.getElementById("service").value = serviceName;
        document.getElementById("booking").scrollIntoView({ behavior: "smooth" });
      } else {
        window.location.hash = "booking";
      }
      return true;
    }

    window.authNextPath = "/index.html#booking";
    window.openAccountModal("signup");
    showError("Please sign up or log in before booking an appointment.");
    return false;
  };

  function openAuthModal(mode) {
    if (typeof window.switchAuthTab === "function") {
      window.switchAuthTab(mode);
    }
    if (typeof window.openModal === "function") {
      window.openModal("accountModal");
    }
    if (mode === "signup" && passwordInput) {
      passwordInput.autocomplete = "new-password";
    } else if (passwordInput) {
      passwordInput.autocomplete = "current-password";
    }
  }

  function safeRedirectTarget() {
    const requestedPath = window.authNextPath || nextPath;
    if (!requestedPath) {
      return "/birth-details.html";
    }

    let target;
    try {
      target = new URL(requestedPath, window.location.origin);
    } catch (error) {
      return "/index.html";
    }

    const allowedPaths = new Set([
      "/index.html",
      "/astro-essentials.html",
      "/birth-details.html",
      "/exclusive-yantra.html",
      "/kundli-report.html",
      "/premium-gemstone.html",
      "/rashi-yantra.html",
      "/standard-gemstone.html"
    ]);
    if (target.origin !== window.location.origin || !allowedPaths.has(target.pathname)) {
      return "/index.html";
    }
    return target.pathname + target.search + target.hash;
  }

  if (form) {
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      if (!form.reportValidity()) {
        return;
      }

      const mode = typeof authMode !== "undefined" ? authMode : "login";
      const body = {
        email: document.getElementById("authEmail").value.trim().toLowerCase(),
        password: passwordInput.value
      };
      if (mode === "signup") {
        body.fullName = nameInput.value.trim();
        if (!body.fullName) {
          showError("Please enter your full name.");
          nameInput.focus();
          return;
        }
      }

      submitButton.disabled = true;
      submitButton.textContent = mode === "signup" ? "Creating account..." : "Signing in...";
      if (errorBox) {
        errorBox.style.display = "none";
      }

      try {
        const result = await requestJson(
          mode === "signup" ? "/api/signup" : "/api/login",
          { method: "POST", body: body }
        );
        setAuthenticatedUser(result.user);
        window.location.assign(safeRedirectTarget());
      } catch (error) {
        showError(error.message);
        submitButton.disabled = false;
        submitButton.textContent = mode === "signup" ? "Sign Up" : "Login";
      }
    });
  }

  if (nameField && nameInput) {
    const signupObserver = new MutationObserver(function () {
      const isSignup = nameField.style.display !== "none";
      nameInput.required = isSignup;
      if (passwordInput) {
        passwordInput.autocomplete = isSignup ? "new-password" : "current-password";
      }
    });
    signupObserver.observe(nameField, { attributes: true, attributeFilter: ["style"] });
  }

  if (accountModal) {
    accountModal.addEventListener("click", function (event) {
      if (event.target === accountModal && typeof window.closeModal === "function") {
        window.closeModal("accountModal");
      }
    });
  }
})();
