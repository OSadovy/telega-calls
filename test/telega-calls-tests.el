;;; telega-calls-tests.el --- Tests for telega-calls  -*- lexical-binding: t -*-

;; Run:  emacs --batch --eval "(setq load-prefer-newer t)" -f package-initialize \
;;         -L . -l test/telega-calls-tests.el \
;;         -f ert-run-tests-batch-and-exit
;;
;; The helper is replaced by `cat', so these cover the Emacs side: what
;; reaches the helper, what reaches TDLib, and what the mode installs.
;; The helper's own logic has Python tests; a real call is tested by hand.

;;; Code:

(require 'ert)
(require 'telega-calls)

(defvar telega-calls-tests--to-helper nil)
(defvar telega-calls-tests--to-tdlib nil)

(defmacro telega-calls-tests--with-fake-helper (&rest body)
  "Run BODY with `cat' as the helper, recording what is sent where."
  (declare (indent 0))
  `(let ((telega-calls-tests--to-helper nil)
         (telega-calls-tests--to-tdlib nil)
         (telega-calls--process nil)
         (telega-calls--call-id nil)
         (telega-calls--early-signals nil)
         (send (symbol-function 'process-send-string)))
     (cl-letf (((symbol-function 'telega-calls--command)
                (lambda (&rest _) (list "cat")))
               ((symbol-function 'process-send-string)
                (lambda (proc string)
                  (if (eq proc telega-calls--process)
                      (dolist (line (split-string string "\n" t))
                        (push (json-parse-string line :object-type 'plist
                                                 :false-object nil)
                              telega-calls-tests--to-helper))
                    (funcall send proc string))))
               ((symbol-function 'telega-server--send)
                (lambda (sexp &rest _) (push sexp telega-calls-tests--to-tdlib))))
       (unwind-protect (progn ,@body)
         (when (process-live-p telega-calls--process)
           (delete-process telega-calls--process))))))

(defun telega-calls-tests--ready-call (id)
  (list :@type "call" :id id :user_id 123456789 :is_outgoing t
        :state (list :@type "callStateReady"
                     :protocol (list :@type "callProtocol" :udp_p2p t
                                     :library_versions ["8.0.0"])
                     :servers (vector (list :@type "callServer" :id "1" :port 597
                                            :type (list :@type "callServerTypeTelegramReflector"
                                                        :peer_tag "AAAA" :is_tcp nil)))
                     :config "{}" :encryption_key "a2V5"
                     ;; As telega-server delivers them: UTF-16 surrogates.
                     :emojis (vector (string #xd83d #xdcb3))
                     :allow_p2p t :custom_parameters "")))

(ert-deftest telega-calls-start-sends-only-the-fields-the-helper-uses ()
  (telega-calls-tests--with-fake-helper
    (telega-calls--start (telega-calls-tests--ready-call 7))
    (let* ((start (car telega-calls-tests--to-helper))
           (state (plist-get start :state)))
      (should (equal (plist-get start :op) "start"))
      (should (equal (plist-get start :user_id) 123456789))
      (should (eq (plist-get start :is_outgoing) t))
      (should (equal (plist-get state :encryption_key) "a2V5"))
      (should (equal (plist-get (plist-get state :protocol) :library_versions) ["8.0.0"]))
      (should (eq (plist-get (plist-get (aref (plist-get state :servers) 0) :type) :is_tcp)
                  nil))
      (should-not (plist-member state :emojis)))))

(ert-deftest telega-calls-signalling-before-start-reaches-helper-in-order ()
  (telega-calls-tests--with-fake-helper
    (telega-calls--on-signaling-data (list :call_id 7 :data "Zmlyc3Q="))
    (telega-calls--on-signaling-data (list :call_id 7 :data "c2Vjb25k"))
    (telega-calls--start (telega-calls-tests--ready-call 7))
    (should (equal (mapcar (lambda (m) (plist-get m :data))
                           (cdr (reverse telega-calls-tests--to-helper)))
                   '("Zmlyc3Q=" "c2Vjb25k")))))

(ert-deftest telega-calls-signalling-for-another-call-is-dropped ()
  (telega-calls-tests--with-fake-helper
    (telega-calls--start (telega-calls-tests--ready-call 7))
    (telega-calls--on-signaling-data (list :call_id 8 :data "b3RoZXI="))
    (telega-calls--on-signaling-data (list :call_id 7 :data "dGhpcw=="))
    (should (equal (plist-get (car telega-calls-tests--to-helper) :data) "dGhpcw=="))
    (should (= (length telega-calls-tests--to-helper) 2))))

(ert-deftest telega-calls-helper-signalling-goes-to-tdlib-for-the-call ()
  (telega-calls-tests--with-fake-helper
    (telega-calls--start (telega-calls-tests--ready-call 7))
    ;; Split mid-line, as pipe reads arrive.
    (telega-calls--filter telega-calls--process "{\"ev\":\"signal\",\"da")
    (should-not telega-calls-tests--to-tdlib)
    (telega-calls--filter telega-calls--process "ta\":\"QUJD\"}\r\n")
    (should (equal telega-calls-tests--to-tdlib
                   '((:@type "sendCallSignalingData" :call_id 7 :data "QUJD"))))))

(ert-deftest telega-calls-start-works-from-a-deleted-directory ()
  (telega-calls-tests--with-fake-helper
    (with-temp-buffer
      (setq default-directory "/nonexistent-telega-calls-test/")
      (telega-calls--start (telega-calls-tests--ready-call 7)))
    (should (process-live-p telega-calls--process))))

(ert-deftest telega-calls-microphone-and-statistics-are-asked-for-only-when-set ()
  (dolist (case '(((nil nil) . nil)
                  (("Desktop" nil) . ("--mic" "Desktop"))
                  ((nil t) . ("--debug"))))
    (let ((telega-calls-microphone (car (car case)))
          (telega-calls-debug (cadr (car case)))
          (args nil))
      (telega-calls-tests--with-fake-helper
        (cl-letf (((symbol-function 'telega-calls--command)
                   (lambda (&rest rest) (setq args rest) (list "cat"))))
          (telega-calls--start (telega-calls-tests--ready-call 7)))
        (should (equal args (cdr case)))))))

(ert-deftest telega-calls-offers-the-helper-protocol-only-while-enabled ()
  (let ((telega-calls--protocol '(:@type "callProtocol" :library_versions ["12.0.0"]))
        (offered nil))
    (cl-letf (((symbol-function 'telega-server--call)
               (lambda (sexp &rest _) (setq offered (plist-get sexp :protocol)))))
      (unwind-protect
          (progn
            (telega-calls-mode 1)
            (telega--createCall '(:id 1))
            (should (equal offered telega-calls--protocol))
            (telega--acceptCall 5)
            (should (equal offered telega-calls--protocol))
            (telega-calls-mode -1)
            (telega--createCall '(:id 1))
            (should (equal offered telega-voip-protocol)))
        (telega-calls-mode -1)))))

(ert-deftest telega-calls-mode-off-removes-what-it-installed ()
  (unwind-protect
      (progn
        (telega-calls-mode 1)
        (should (memq #'telega-calls--start-safely telega-call-ready-hook))
        (should (eq (symbol-function 'telega--on-updateNewCallSignalingData)
                    'telega-calls--on-signaling-data))
        (should (eq (lookup-key telega-voip-map (kbd "m")) #'telega-calls-toggle-mute))
        (telega-calls-mode -1)
        (should-not (memq #'telega-calls--start-safely telega-call-ready-hook))
        (should-not (memq #'telega-calls--stop telega-call-end-hook))
        (should-not (fboundp 'telega--on-updateNewCallSignalingData))
        (should-not (lookup-key telega-voip-map (kbd "m")))
        (should-not (advice-member-p #'telega-calls--offer-helper-protocol
                                     'telega--createCall)))
    (telega-calls-mode -1)))

(ert-deftest telega-calls-mode-off-leaves-a-handler-telega-defined ()
  (let ((theirs (lambda (_event) 'telega)))
    (unwind-protect
        (progn
          (telega-calls-mode 1)
          (defalias 'telega--on-updateNewCallSignalingData theirs)
          (telega-calls-mode -1)
          (should (eq (symbol-function 'telega--on-updateNewCallSignalingData) theirs)))
      (fmakunbound 'telega--on-updateNewCallSignalingData))))

(ert-deftest telega-calls-windows-interpreter-gets-windows-paths ()
  (let ((telega-calls-python "/mnt/c/venv/Scripts/python.exe"))
    (if (executable-find "wslpath")
        (should (string-prefix-p "\\\\wsl" (telega-calls--path telega-calls--directory)))
      (should (equal (telega-calls--path "/tmp") "/tmp"))))
  (let ((telega-calls-python "/usr/bin/python3"))
    (should (equal (telega-calls--path "/tmp/x") "/tmp/x"))))

;;; telega-calls-tests.el ends here
