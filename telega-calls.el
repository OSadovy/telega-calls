;;; telega-calls.el --- Voice calls for telega through an external media helper  -*- lexical-binding: t -*-

;; Copyright (C) 2026 Oleksii Sadovyi

;; Author: Oleksii Sadovyi <lex.sadovyi@gmail.com>
;; Version: 0.1.0
;; Package-Requires: ((emacs "29.1") (telega "0.8.660"))
;; Keywords: comm

;; This program is free software: you can redistribute it and/or modify
;; it under the terms of the GNU General Public License as published by
;; the Free Software Foundation, either version 3 of the License, or
;; (at your option) any later version.

;; This program is distributed in the hope that it will be useful,
;; but WITHOUT ANY WARRANTY; without even the implied warranty of
;; MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
;; GNU General Public License for more details.

;; You should have received a copy of the GNU General Public License
;; along with this program.  If not, see <https://www.gnu.org/licenses/>.

;;; Commentary:

;; telega places, answers and hangs up private calls through TDLib, but the
;; media engine it can be built with, libtgvoip, no longer talks to current
;; Telegram apps.  `telega-calls-mode' gives the media of each call to a
;; helper program instead, and relays the call's signalling between the
;; helper and TDLib.  The bundled helper runs NTgCalls; any program speaking
;; the JSON-lines protocol described in helper/telega_calls_helper.py will do.
;;
;; Calls are placed and answered with telega's own commands; this adds
;; `telega-calls-toggle-mute', on "m" in telega's call keys.

;;; Code:

(require 'json)
(require 'telega)

(defgroup telega-calls nil
  "Voice calls for telega through an external media helper."
  :group 'telega)

(defconst telega-calls--directory
  (file-name-directory (or load-file-name buffer-file-name)))

(defcustom telega-calls-python nil
  "Python interpreter that runs the helper, with its requirements installed.
From WSL this is a Windows interpreter, such as
\"/mnt/c/Users/me/telega-calls/venv/Scripts/python.exe\"."
  :type '(choice (const :tag "Not set" nil) file))

(defcustom telega-calls-helper
  (expand-file-name "helper/telega_calls_helper.py" telega-calls--directory)
  "The helper script."
  :type 'file)

(defcustom telega-calls-microphone nil
  "Part of the name of the microphone the helper captures; nil for the default.
The default is followed, not fixed: a call started after it changes uses
the new one."
  :type '(choice (const :tag "System default" nil) string))

(defcustom telega-calls-log-directory nil
  "Directory for NTgCalls' and WebRTC's logs, one file per call; nil for none."
  :type '(choice (const :tag "No logs" nil) directory))

(defcustom telega-calls-trace nil
  "Non-nil logs NTgCalls and WebRTC in full; needs `telega-calls-log-directory'."
  :type 'boolean)

(defcustom telega-calls-debug nil
  "Non-nil logs the helper's debugging messages in *telega-calls*."
  :type 'boolean)

(defvar telega-calls--protocol nil
  "The callProtocol the helper supports, asked of it once.")
(defvar telega-calls--process nil)
(defvar telega-calls--call-id nil)
(defvar telega-calls--muted nil)
(defvar telega-calls--early-signals nil
  "Signalling TDLib delivered before the helper started, newest first.")

(defmacro telega-calls--from-home (&rest body)
  "Run BODY with a `default-directory' processes can start from.
telega's event handlers run in a buffer whose directory may be remote or
no longer exist."
  `(let ((default-directory telega-calls--directory))
     ,@body))

(defun telega-calls--log (fmt &rest args)
  (with-current-buffer (get-buffer-create "*telega-calls*")
    (save-excursion
      (goto-char (point-max))
      (insert (format-time-string "%T.%3N ") (apply #'format fmt args) "\n"))))

(defun telega-calls--path (file)
  "FILE as `telega-calls-python' sees it.
A Windows interpreter run from WSL needs a Windows path."
  (if (and (string-suffix-p ".exe" telega-calls-python)
           (executable-find "wslpath"))
      (car (telega-calls--from-home
            (process-lines "wslpath" "-w" (expand-file-name file))))
    (expand-file-name file)))

(defun telega-calls--command (&rest args)
  (unless telega-calls-python
    (user-error "Set `telega-calls-python' to the helper's interpreter"))
  (append (list telega-calls-python "-u" (telega-calls--path telega-calls-helper))
          args))

(defun telega-calls--protocol ()
  "The callProtocol to offer TDLib: what the helper supports."
  (or telega-calls--protocol
      (setq telega-calls--protocol
            (append (list :@type "callProtocol")
                    (json-parse-string
                     (car (last (telega-calls--from-home
                                 (apply #'process-lines
                                        (telega-calls--command "--protocol")))))
                     :object-type 'plist :false-object :false)))))

(defun telega-calls--offer-helper-protocol (fn &rest args)
  "Apply FN to ARGS with the helper's protocol offered instead of libtgvoip's."
  (let ((telega-voip-protocol (telega-calls--protocol)))
    (apply fn args)))

(defun telega-calls--send (&rest msg)
  (if (process-live-p telega-calls--process)
      (process-send-string telega-calls--process
                           (concat (json-serialize msg :false-object nil) "\n"))
    (telega-calls--log "not sent, helper not running: %s" (plist-get msg :op))))

(defun telega-calls--handle (msg)
  (pcase (plist-get msg :ev)
    ("signal"
     (telega-server--send (list :@type "sendCallSignalingData"
                                :call_id telega-calls--call-id
                                :data (plist-get msg :data))))
    ("ready"
     (telega-calls--log "helper ready: microphone %s, output %s"
                        (plist-get msg :mic) (plist-get msg :speaker)))
    ("state"
     (let ((state (downcase (plist-get msg :state))))
       (telega-calls--log "audio %s" state)
       (message "Call audio %s" state)))
    ("error"
     (telega-calls--log "error in %s: %s"
                        (plist-get msg :op) (plist-get msg :message))
     (message "Call audio error: %s" (plist-get msg :message)))
    (_ (telega-calls--log "%S" msg))))

(defun telega-calls--filter (proc output)
  (let ((pending (concat (process-get proc 'pending) output)))
    (while (string-match "\n" pending)
      (let ((line (string-trim-right (substring pending 0 (match-beginning 0)))))
        (setq pending (substring pending (match-end 0)))
        (condition-case err
            (telega-calls--handle
             (json-parse-string line :object-type 'plist :false-object nil))
          (error (telega-calls--log "unreadable %S: %S" line err)))))
    (process-put proc 'pending pending)))

(defun telega-calls--sentinel (proc event)
  (telega-calls--log "helper %s" (string-trim event))
  (when (and (eq proc telega-calls--process) telega-calls--call-id)
    (message "Call audio helper stopped: %s" (string-trim event))))

(defun telega-calls--start (call)
  "Start the helper for CALL, which TDLib reports ready."
  (let ((early (reverse telega-calls--early-signals)))
    (telega-calls--stop)
    (setq telega-calls--early-signals early))
  (setq telega-calls--call-id (plist-get call :id)
        telega-calls--muted nil)
  (setq telega-calls--process
        (telega-calls--from-home
         (make-process
          :name "telega-calls"
          :command (apply #'telega-calls--command
                          (append
                           (when telega-calls-microphone
                             (list "--mic" telega-calls-microphone))
                           (when telega-calls-log-directory
                             (make-directory telega-calls-log-directory t)
                             (list "--log-dir"
                                   (telega-calls--path telega-calls-log-directory)))
                           (when telega-calls-trace (list "--trace"))
                           (when telega-calls-debug (list "--debug"))))
          :connection-type 'pipe
          :coding 'utf-8-unix
          :noquery t
          :stderr (get-buffer-create "*telega-calls*")
          :filter #'telega-calls--filter
          :sentinel #'telega-calls--sentinel)))
  (telega-calls--log "call %s ready, helper started" telega-calls--call-id)
  ;; Only the fields the helper uses: telega-server hands :emojis over as
  ;; UTF-16 surrogates, which `json-serialize' rejects.
  (let ((state (plist-get call :state)))
    (telega-calls--send :op "start"
                        :user_id (plist-get call :user_id)
                        :is_outgoing (plist-get call :is_outgoing)
                        :state (list :protocol (plist-get state :protocol)
                                     :servers (plist-get state :servers)
                                     :encryption_key (plist-get state :encryption_key)
                                     :allow_p2p (plist-get state :allow_p2p)
                                     :custom_parameters (plist-get state :custom_parameters))))
  (dolist (data telega-calls--early-signals)
    (telega-calls--send :op "signal" :data data))
  (setq telega-calls--early-signals nil))

(defun telega-calls--start-safely (call)
  "Start the helper for CALL; an error must not stop telega's other ready hooks."
  (condition-case err
      (telega-calls--start call)
    (error (telega-calls--log "start failed: %S" err)
           (message "Call audio failed to start: %s" (error-message-string err)))))

(defun telega-calls--stop (&rest _)
  "Stop the helper, giving it a moment to hang up cleanly."
  (when (process-live-p telega-calls--process)
    (telega-calls--send :op "stop")
    (let ((proc telega-calls--process))
      (run-at-time 3 nil (lambda ()
                           (when (process-live-p proc)
                             (delete-process proc))))))
  (setq telega-calls--process nil
        telega-calls--call-id nil
        telega-calls--early-signals nil))

(defun telega-calls--on-signaling-data (event)
  "Pass the other side's signalling in EVENT to the helper."
  (let ((data (plist-get event :data)))
    (if (process-live-p telega-calls--process)
        (when (eql (plist-get event :call_id) telega-calls--call-id)
          (telega-calls--send :op "signal" :data data))
      (push data telega-calls--early-signals))))

(defun telega-calls-toggle-mute ()
  "Mute or unmute the microphone in the current call."
  (interactive)
  (unless (process-live-p telega-calls--process)
    (user-error "No call audio running"))
  (setq telega-calls--muted (not telega-calls--muted))
  (telega-calls--send :op (if telega-calls--muted "mute" "unmute"))
  (message "Microphone %s" (if telega-calls--muted "muted" "on")))

;;;###autoload
(define-minor-mode telega-calls-mode
  "Carry the audio of telega's private calls through `telega-calls-helper'."
  :global t
  :group 'telega-calls
  (if telega-calls-mode
      (progn
        (advice-add 'telega--createCall :around #'telega-calls--offer-helper-protocol)
        (advice-add 'telega--acceptCall :around #'telega-calls--offer-helper-protocol)
        (add-hook 'telega-call-ready-hook #'telega-calls--start-safely)
        (add-hook 'telega-call-end-hook #'telega-calls--stop)
        ;; telega dispatches each TDLib update to the function named after
        ;; it, and has none for this one.
        (defalias 'telega--on-updateNewCallSignalingData
          #'telega-calls--on-signaling-data)
        (define-key telega-voip-map (kbd "m") #'telega-calls-toggle-mute))
    (telega-calls--stop)
    (advice-remove 'telega--createCall #'telega-calls--offer-helper-protocol)
    (advice-remove 'telega--acceptCall #'telega-calls--offer-helper-protocol)
    (remove-hook 'telega-call-ready-hook #'telega-calls--start-safely)
    (remove-hook 'telega-call-end-hook #'telega-calls--stop)
    (when (eq (symbol-function 'telega--on-updateNewCallSignalingData)
              'telega-calls--on-signaling-data)
      (fmakunbound 'telega--on-updateNewCallSignalingData))
    (when (eq (lookup-key telega-voip-map (kbd "m")) #'telega-calls-toggle-mute)
      (define-key telega-voip-map (kbd "m") nil))))

(provide 'telega-calls)
;;; telega-calls.el ends here
