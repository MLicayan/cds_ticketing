document.addEventListener("DOMContentLoaded", function () {
  const config = window.KANBAN_CONFIG || {};

  const canDragKanban = config.canDrag === true;
  const willPromptClientOnResolve =
    config.willPromptClientOnResolve === true;

  let draggedCard = null;
  let pendingKanbanDrop = null;

  const filterBy = document.getElementById("filter_by");
  const engineerGroup = document.getElementById(
    "engineer_filter_group"
  );
  const engineerTypeGroup = document.getElementById(
    "engineer_type_filter_group"
  );
  const clientGroup = document.getElementById(
    "client_filter_group"
  );

  const engineerSelect = document.getElementById(
    "filter_value_engineer"
  );
  const engineerTypeSelect = document.getElementById(
    "filter_user_type"
  );
  const clientSelect = document.getElementById(
    "filter_value_client"
  );

  const confirmResolvePromptButton = document.getElementById(
    "confirmKanbanResolvePromptBtn"
  );
  const ticketTasksModal = document.getElementById(
    "kanbanTicketTasksModal"
  );
  const ticketTasksModalTitle = document.getElementById(
    "kanbanTicketTasksModalTitle"
  );
  const ticketTasksModalBody = document.getElementById(
    "kanbanTicketTasksModalBody"
  );

  /**
   * Filter engineer options by selected user type.
   */
  function syncEngineerOptions() {
    if (!engineerSelect || !engineerTypeSelect) {
      return;
    }

    const selectedType = engineerTypeSelect.value;
    const currentValue =
      engineerSelect.getAttribute("data-current") ||
      engineerSelect.value ||
      "";

    Array.from(engineerSelect.options).forEach(function (
      option,
      index
    ) {
      if (index === 0 || option.value === "unassigned") {
        option.hidden = false;
        return;
      }

      const optionType =
        option.getAttribute("data-user-type") || "";

      option.hidden =
        selectedType !== "" && optionType !== selectedType;
    });

    const selectedOption =
      engineerSelect.options[engineerSelect.selectedIndex];

    if (selectedOption && selectedOption.hidden) {
      engineerSelect.value = "";
      return;
    }

    if (currentValue) {
      const matchingOption = Array.from(
        engineerSelect.options
      ).find(function (option) {
        return (
          option.value === currentValue &&
          option.hidden === false
        );
      });

      if (matchingOption) {
        engineerSelect.value = currentValue;
      }
    }
  }

  /**
   * Show or hide filter controls.
   */
  function syncFilterControls() {
    if (
      !filterBy ||
      !engineerGroup ||
      !engineerTypeGroup ||
      !clientGroup ||
      !engineerSelect ||
      !engineerTypeSelect ||
      !clientSelect
    ) {
      return;
    }

    const mode = filterBy.value;

    const assignedMode = mode === "assigned";
    const clientMode = mode === "client";

    engineerGroup.style.display = assignedMode ? "" : "none";
    engineerTypeGroup.style.display = assignedMode
      ? ""
      : "none";
    clientGroup.style.display = clientMode ? "" : "none";

    engineerSelect.disabled = !assignedMode;
    engineerTypeSelect.disabled = !assignedMode;
    clientSelect.disabled = !clientMode;

    if (!assignedMode) {
      engineerSelect.value = "";
      engineerTypeSelect.value = "";
    }

    if (!clientMode) {
      clientSelect.value = "";
    }

    syncEngineerOptions();
  }

  if (engineerTypeSelect) {
    engineerTypeSelect.addEventListener(
      "change",
      function () {
        if (engineerSelect) {
          engineerSelect.value = "";
          engineerSelect.setAttribute("data-current", "");
        }

        syncEngineerOptions();
      }
    );
  }

  if (filterBy) {
    filterBy.addEventListener(
      "change",
      syncFilterControls
    );

    syncFilterControls();
  } else {
    syncEngineerOptions();
  }

  function getCardNavigationTarget(card, mode) {
    if (!card) {
      return "";
    }

    if (mode === "single") {
      return (
        card.getAttribute("data-single-click-href") || ""
      );
    }

    if (mode === "double") {
      return (
        card.getAttribute("data-double-click-href") || ""
      );
    }

    return card.getAttribute("data-new-tab-href") || "";
  }

  function getTicketTasks(card) {
    if (!card) {
      return [];
    }

    const taskSource = card.querySelector(
      ".kanban-ticket-tasks-source"
    );

    if (!taskSource) {
      return [];
    }

    return Array.from(
      taskSource.querySelectorAll(".kanban-ticket-task-item")
    ).map(function (taskNode) {
      return {
        taskNo:
          taskNode.getAttribute("data-task-no") || "N/A",
        subject:
          taskNode.getAttribute("data-subject") ||
          "No subject",
        status:
          taskNode.getAttribute("data-status") ||
          "No status",
        assignee:
          taskNode.getAttribute("data-assignee") ||
          "Unassigned",
        url: taskNode.getAttribute("data-url") || "#"
      };
    });
  }

  function renderTicketTasksModal(card) {
    if (
      !card ||
      !ticketTasksModal ||
      !ticketTasksModalBody ||
      !ticketTasksModalTitle ||
      !window.jQuery ||
      !window.jQuery(ticketTasksModal).modal
    ) {
      return false;
    }

    const ticketNo =
      card.getAttribute("data-ticket-no") || "Ticket";
    const ticketSubject =
      card.getAttribute("data-ticket-subject") || "No subject";
    const tasks = getTicketTasks(card);

    ticketTasksModalTitle.innerHTML =
      '<i class="fas fa-tasks mr-2 text-primary"></i>' +
      ticketNo +
      " Tasks";

    ticketTasksModalBody.innerHTML = "";

    if (!tasks.length) {
      const emptyState = document.createElement("p");
      emptyState.className = "kanban-task-list-empty";
      emptyState.textContent =
        'No tasks found for "' + ticketSubject + '".';
      ticketTasksModalBody.appendChild(emptyState);
    } else {
      tasks.forEach(function (task) {
        const taskLink = document.createElement("a");
        taskLink.className = "kanban-task-list-item";
        taskLink.href = task.url || "#";
        const normalizedStatus = (
          task.status || ""
        ).toLowerCase();
        if (normalizedStatus === "fix/completed") {
          taskLink.classList.add("is-resolved");
        }

        const meta = document.createElement("div");
        meta.className = "kanban-task-list-meta";

        const number = document.createElement("span");
        number.textContent = task.taskNo || "N/A";

        const status = document.createElement("span");
        status.className =
          "badge " +
          (normalizedStatus === "fix/completed"
            ? "kanban-task-status-resolved"
            : "badge-light");
        status.textContent = task.status || "No status";

        meta.appendChild(number);
        meta.appendChild(status);

        const subject = document.createElement("p");
        subject.className = "kanban-task-list-subject";
        subject.textContent = task.subject || "No subject";

        const assignee = document.createElement("div");
        assignee.className = "kanban-task-list-assignee";
        assignee.textContent =
          "Assigned to: " + (task.assignee || "Unassigned");

        taskLink.appendChild(meta);
        taskLink.appendChild(subject);
        taskLink.appendChild(assignee);
        ticketTasksModalBody.appendChild(taskLink);
      });
    }

    window.jQuery(ticketTasksModal).modal("show");
    return true;
  }

  /**
   * Ticket cards open task list on single click and ticket detail on double
   * click. Task cards continue to open task detail.
   */
  document
    .querySelectorAll(".kanban-card[data-single-click-href]")
    .forEach(function (card) {
      let clickTimer = null;
      const cardKind =
        card.getAttribute("data-card-kind") || "task";

      card.addEventListener("click", function (event) {
        if (
          event.target.closest(
            "a, button, input, select, textarea, label"
          )
        ) {
          return;
        }

        if (card.classList.contains("is-dragging")) {
          return;
        }

        window.clearTimeout(clickTimer);
        clickTimer = window.setTimeout(function () {
          if (cardKind === "ticket") {
            renderTicketTasksModal(card);
            return;
          }

          const href = getCardNavigationTarget(card, "single");
          if (href) {
            window.location.href = href;
          }
        }, 220);
      });

      card.addEventListener("dblclick", function (event) {
        if (
          event.target.closest(
            "a, button, input, select, textarea, label"
          )
        ) {
          return;
        }

        if (card.classList.contains("is-dragging")) {
          return;
        }

        window.clearTimeout(clickTimer);

        const href = getCardNavigationTarget(card, "double");

        if (href) {
          window.location.href = href;
        }
      });

      card.addEventListener("contextmenu", function (event) {
        if (
          event.target.closest(
            "a, button, input, select, textarea, label"
          )
        ) {
          return;
        }

        if (card.classList.contains("is-dragging")) {
          return;
        }

        const href = getCardNavigationTarget(card, "tab");

        if (href) {
          event.preventDefault();
          window.open(href, "_blank", "noopener");
        }
      });

      card.addEventListener("keydown", function (event) {
        if (event.key !== "Enter" && event.key !== " ") {
          return;
        }

        event.preventDefault();

        if (cardKind === "ticket") {
          renderTicketTasksModal(card);
          return;
        }

        const href = getCardNavigationTarget(card, "single");

        if (href) {
          window.location.href = href;
        }
      });
    });

  if (!canDragKanban) {
    console.log("Kanban dragging is disabled for this user.");
    return;
  }

  /**
   * Submit the new status to Flask.
   */
  function completeKanbanDrop(dropData) {
    if (!dropData) {
      return;
    }

    const formData = new FormData();
    formData.append("column", dropData.targetStatus);

    fetch(dropData.updateUrl, {
      method: "POST",
      body: formData,
      credentials: "same-origin",
      headers: {
        "X-Requested-With": "XMLHttpRequest"
      }
    })
      .then(function (response) {
        if (!response.ok) {
          return response.text().then(function (text) {
            throw new Error(
              "Kanban update failed: " +
                response.status +
                " " +
                text
            );
          });
        }

        return response.json();
      })
      .then(function (payload) {
        if (!payload || payload.ok !== true) {
          throw new Error(
            payload && payload.message
              ? payload.message
              : "The server did not accept the status update."
          );
        }

        if (
          dropData.targetStatus === "resolved" &&
          willPromptClientOnResolve &&
          payload.client_prompt_sent &&
          window.jQuery &&
          window.jQuery("#kanbanResolveSentModal").modal
        ) {
          window
            .jQuery("#kanbanResolveSentModal")
            .modal("show");

          return;
        }

        window.location.reload();
      })
      .catch(function (error) {
        console.error(error);
        alert(
          "Unable to move the card.\n\n" + error.message
        );
      });
  }

  /**
   * Register draggable cards.
   */
  document
    .querySelectorAll(
      '.kanban-card[draggable="true"]'
    )
    .forEach(function (card) {
      card.addEventListener(
        "dragstart",
        function (event) {
          draggedCard = card;

          card.classList.add("is-dragging");

          event.dataTransfer.effectAllowed = "move";

          event.dataTransfer.setData(
            "text/plain",
            card.getAttribute("data-ticket-id") || ""
          );

          setTimeout(function () {
            card.style.opacity = "0.55";
          }, 0);
        }
      );

      card.addEventListener("dragend", function () {
        card.classList.remove("is-dragging");
        card.style.opacity = "";

        draggedCard = null;

        document
          .querySelectorAll(
            ".kanban-cards.is-drop-target"
          )
          .forEach(function (zone) {
            zone.classList.remove("is-drop-target");
          });
      });
    });

  /**
   * Register drop zones.
   */
  document
    .querySelectorAll(".kanban-cards[data-status]")
    .forEach(function (zone) {
      zone.addEventListener(
        "dragenter",
        function (event) {
          event.preventDefault();

          if (draggedCard) {
            zone.classList.add("is-drop-target");
          }
        }
      );

      zone.addEventListener(
        "dragover",
        function (event) {
          event.preventDefault();

          event.dataTransfer.dropEffect = "move";

          if (draggedCard) {
            zone.classList.add("is-drop-target");
          }
        }
      );

      zone.addEventListener(
        "dragleave",
        function (event) {
          if (!zone.contains(event.relatedTarget)) {
            zone.classList.remove("is-drop-target");
          }
        }
      );

      zone.addEventListener("drop", function (event) {
        event.preventDefault();
        event.stopPropagation();

        zone.classList.remove("is-drop-target");

        if (!draggedCard) {
          return;
        }

        const ticketId =
          draggedCard.getAttribute("data-ticket-id");

        const currentStatus =
          draggedCard.getAttribute("data-status");

        const targetStatus =
          zone.getAttribute("data-status");

        const updateUrl =
          draggedCard.getAttribute("data-update-url");

        if (!ticketId) {
          alert("The card has no ticket ID.");
          return;
        }

        if (!updateUrl) {
          alert("The card has no update URL.");
          return;
        }

        if (!targetStatus) {
          alert("The destination column has no status.");
          return;
        }

        if (currentStatus === targetStatus) {
          return;
        }

        const dropData = {
          ticketId: ticketId,
          updateUrl: updateUrl,
          targetStatus: targetStatus
        };

        if (
          targetStatus === "resolved" &&
          willPromptClientOnResolve &&
          window.jQuery &&
          window.jQuery("#kanbanResolvePromptModal").modal
        ) {
          pendingKanbanDrop = dropData;

          window
            .jQuery("#kanbanResolvePromptModal")
            .modal("show");

          return;
        }

        completeKanbanDrop(dropData);
      });
    });

  if (confirmResolvePromptButton) {
    confirmResolvePromptButton.addEventListener(
      "click",
      function () {
        const dropData = pendingKanbanDrop;

        pendingKanbanDrop = null;

        if (
          window.jQuery &&
          window.jQuery("#kanbanResolvePromptModal").modal
        ) {
          window
            .jQuery("#kanbanResolvePromptModal")
            .modal("hide");
        }

        completeKanbanDrop(dropData);
      }
    );
  }

  if (
    window.jQuery &&
    window.jQuery("#kanbanResolvePromptModal").modal
  ) {
    window
      .jQuery("#kanbanResolvePromptModal")
      .on("hidden.bs.modal", function () {
        pendingKanbanDrop = null;
      });
  }

  if (
    window.jQuery &&
    window.jQuery("#kanbanResolveSentModal").modal
  ) {
    window
      .jQuery("#kanbanResolveSentModal")
      .on("hidden.bs.modal", function () {
        window.location.reload();
      });
  }
});
